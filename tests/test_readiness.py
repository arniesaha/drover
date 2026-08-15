"""``/readyz`` has to mean "this instance can serve", not "the process is up".

Issue #175. During the 2026-08-14 outage the hub answered ``/readyz`` with
``200 ok`` for hours while every query raised::

    FATAL Error: Failed: database has been invalidated because of a previous
    fatal error. The database must be restarted prior to being used again.

Once a DuckDB instance is invalidated every later query fails until the
process restarts -- but the process stays alive, launchd reports status 0,
and readiness kept saying healthy. These tests pin the two halves of the fix:
a hub whose handles answer is ready, and a hub holding a handle that no longer
answers is not, for either store.
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import duckdb
import pytest

from drover.schema import bootstrap
from drover.server.db import (
    close_control_plane_connections,
    control_plane_connection,
    control_plane_path,
    live_connection,
    open_duckdb_connection,
    remember_live_connection,
)
from drover.server.metrics import MetricsCollector, start_metrics_server
from drover.server.web.auth import AuthSettings
from drover.server.readiness import (
    STATE_ABSENT,
    STATE_BUSY,
    STATE_FAILED,
    STATE_IDLE,
    STATE_OK,
    STORE_ANALYTICAL,
    STORE_CONTROL_PLANE,
    ReadinessProbe,
)


@pytest.fixture(autouse=True)
def _release_pins():
    """Never leak a pinned control-plane connection into the next test."""
    yield
    close_control_plane_connections()


def _db(tmp_path: Path) -> Path:
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    return duckdb_path


def _collector(duckdb_path: Path) -> MetricsCollector:
    return MetricsCollector(
        duckdb_path=duckdb_path,
        incoming_dir=duckdb_path.parent / "incoming",
        summarizer_report={},
        ttl_seconds=60,
    )


def _get(port: int, path: str) -> tuple[int, str]:
    """GET one endpoint, returning the status even when it is a failure."""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}{path}", timeout=5
        ) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def _serve(duckdb_path: Path):
    collector = _collector(duckdb_path)
    server = start_metrics_server(host="127.0.0.1", port=0, collector=collector)
    return server, server.server_address[1]


def _states(body: str) -> dict[str, str]:
    payload = json.loads(body)
    return {store["store"]: store["state"] for store in payload["stores"]}


class _InvalidatedConnection:
    """A handle whose queries fail the way an invalidated DuckDB one does.

    Nothing in DuckDB 1.5 lets a test invalidate an instance on purpose:
    a corrupt block and a full disk were both measured to raise IOException
    and TransactionException *without* invalidating, and invalidation is set
    only for FATAL/INTERNAL errors inside ``ClientContext``. So the fatal
    error itself is the one simulated part of this test -- the borrowed
    handle, the probe, the classification and the HTTP response are all the
    real code path, and the message is verbatim from the outage.
    """

    MESSAGE = (
        "FATAL Error: Failed: database has been invalidated because of a "
        "previous fatal error. The database must be restarted prior to being "
        "used again."
    )

    def cursor(self):
        return self

    def execute(self, *args, **kwargs):
        raise duckdb.FatalException(self.MESSAGE)

    def close(self) -> None:
        return None


def test_a_healthy_hub_is_ready(tmp_path):
    """Both stores answer, so readiness is 200 and says which stores it proved."""
    duckdb_path = _db(tmp_path)
    con = open_duckdb_connection(duckdb_path, role="diagnostic")
    server, port = _serve(duckdb_path)
    try:
        status, body = _get(port, "/readyz")
    finally:
        server.shutdown()
        con.close()

    assert status == 200
    payload = json.loads(body)
    assert payload["ready"] is True
    assert _states(body) == {
        STORE_ANALYTICAL: STATE_OK,
        STORE_CONTROL_PLANE: STATE_OK,
    }


def test_an_invalidated_analytical_handle_fails_readiness(tmp_path):
    """The outage itself: a live handle that no longer answers must be 503.

    The probe borrows whatever analytical connection this process is holding,
    so an invalidated instance is reached through exactly the handle every
    worker is failing on. The handle is registered the same way a worker's is.
    """
    duckdb_path = _db(tmp_path)
    invalidated = _InvalidatedConnection()
    remember_live_connection(duckdb_path, invalidated)
    server, port = _serve(duckdb_path)
    try:
        status, body = _get(port, "/readyz")
    finally:
        server.shutdown()

    assert status == 503
    payload = json.loads(body)
    assert payload["ready"] is False
    assert _states(body)[STORE_ANALYTICAL] == STATE_FAILED
    detail = next(
        store["detail"]
        for store in payload["stores"]
        if store["store"] == STORE_ANALYTICAL
    )
    assert "invalidated" in detail


def test_an_anonymous_caller_gets_the_verdict_but_not_the_error(tmp_path):
    """``/readyz`` stays public, so its body must stay quiet about the store.

    A monitor cannot hold a credential, which is why this route is one of the
    few unauthenticated ones -- but DuckDB's errors quote the store's
    filesystem path, and the status code is the part an anonymous caller
    needs.
    """
    duckdb_path = _db(tmp_path)
    invalidated = _InvalidatedConnection()
    remember_live_connection(duckdb_path, invalidated)
    collector = _collector(duckdb_path)
    auth = AuthSettings(enabled=True, api_token="readiness-test-token")
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=auth
    )
    try:
        status, body = _get(server.server_address[1], "/readyz")
    finally:
        server.shutdown()

    assert status == 503
    payload = json.loads(body)
    assert payload["ready"] is False
    assert _states(body)[STORE_ANALYTICAL] == STATE_FAILED
    assert all(store["detail"] == "" for store in payload["stores"])


def test_a_dead_control_plane_pin_fails_readiness(tmp_path, monkeypatch):
    """A control-plane handle that died must fail readiness too.

    Either store can be invalidated independently, and ``/harness*`` is served
    from this one, so a probe that only checked the lakehouse would still be
    lying. No simulation here: the pinned connection is genuinely closed, the
    way ``test_a_broken_control_plane_connection_is_replaced_on_the_next_read``
    does it.
    """
    monkeypatch.setenv("DROVER_CONTROL_PLANE_PIN", "1")
    duckdb_path = _db(tmp_path)
    from drover.server.db import pin_control_plane_connection

    assert pin_control_plane_connection(duckdb_path) is True
    with control_plane_connection(duckdb_path) as pinned:
        pinned.close()

    server, port = _serve(duckdb_path)
    try:
        status, body = _get(port, "/readyz")
    finally:
        server.shutdown()

    assert status == 503
    assert _states(body)[STORE_CONTROL_PLANE] == STATE_FAILED


def test_a_corrupt_control_plane_store_fails_readiness(tmp_path):
    """A store that cannot be opened at all is the other half of #171."""
    duckdb_path = _db(tmp_path)
    control_plane_path(duckdb_path).write_bytes(b"not a duckdb file" * 512)

    server, port = _serve(duckdb_path)
    try:
        status, body = _get(port, "/readyz")
    finally:
        server.shutdown()

    assert status == 503
    assert _states(body)[STORE_CONTROL_PLANE] == STATE_FAILED


def test_healthz_stays_up_while_readiness_is_down(tmp_path):
    """Liveness is unchanged: the process is running, it just cannot serve.

    Restart logic keys off the difference, so ``/healthz`` must not learn
    about the database at all.
    """
    duckdb_path = _db(tmp_path)
    invalidated = _InvalidatedConnection()
    remember_live_connection(duckdb_path, invalidated)
    server, port = _serve(duckdb_path)
    try:
        ready_status, _ = _get(port, "/readyz")
        health_status, health_body = _get(port, "/healthz")
    finally:
        server.shutdown()

    assert ready_status == 503
    assert health_status == 200
    assert health_body == "ok\n"


def test_readiness_opens_no_analytical_connection(tmp_path, monkeypatch):
    """The probe must never be a new source of connect contention.

    ``duckdb.connect`` on a saturated instance is where ``sample(1)`` found
    threads parked during #95, and ``/readyz`` may be polled every few
    seconds. Borrowing a cursor from a handle the process already holds costs
    ~70us and takes no lock at all.
    """
    monkeypatch.setenv("DROVER_CONTROL_PLANE_PIN", "1")
    duckdb_path = _db(tmp_path)
    from drover.server.db import pin_control_plane_connection

    assert pin_control_plane_connection(duckdb_path) is True
    con = open_duckdb_connection(duckdb_path, role="diagnostic")
    try:
        assert live_connection(duckdb_path) is not None

        def explode(*args, **kwargs):
            raise AssertionError("the readiness probe opened a DuckDB connection")

        monkeypatch.setattr(duckdb, "connect", explode)
        report = ReadinessProbe(duckdb_path, cache_seconds=0.0).check()
    finally:
        con.close()

    assert report.ok
    assert {store.store: store.state for store in report.stores} == {
        STORE_ANALYTICAL: STATE_OK,
        STORE_CONTROL_PLANE: STATE_OK,
    }


def test_a_hub_holding_no_analytical_handle_is_still_ready(tmp_path):
    """No handle open means no handle can be invalid.

    The hub's analytical readers connect per unit of work, so between them the
    process holds nothing; the next connect builds a fresh instance, which is
    by definition not an invalidated one. Reporting that as unready would make
    an idle hub look broken.
    """
    duckdb_path = _db(tmp_path)

    report = ReadinessProbe(duckdb_path, cache_seconds=0.0).check()

    assert report.ok
    assert {store.store: store.state for store in report.stores}[
        STORE_ANALYTICAL
    ] == STATE_IDLE


def test_a_closed_handle_is_not_an_outage(tmp_path):
    """A finished connection that has not been collected yet is housekeeping.

    ``LedgerWriter`` and friends hold their connection on an attribute, so a
    closed-but-referenced handle is reachable. It proves nothing about the
    instance, and turning it red would be a false alarm every shutdown.
    """
    duckdb_path = _db(tmp_path)
    finished = open_duckdb_connection(duckdb_path, role="diagnostic")
    finished.close()

    report = ReadinessProbe(duckdb_path, cache_seconds=0.0).check()

    assert report.ok, report.as_dict()


def test_missing_stores_are_reported_rather_than_created(tmp_path):
    """A hub that has not bootstrapped yet must not be probed into existence.

    Connecting to a missing DuckDB path creates it, and creating the
    control-plane store from a readiness poll would be a side effect nobody
    asked for.
    """
    duckdb_path = tmp_path / "drover.duckdb"

    report = ReadinessProbe(duckdb_path, cache_seconds=0.0).check()

    assert report.ok
    assert {store.store: store.state for store in report.stores} == {
        STORE_ANALYTICAL: STATE_IDLE,
        STORE_CONTROL_PLANE: STATE_ABSENT,
    }
    assert not control_plane_path(duckdb_path).exists()


def test_another_process_holding_the_store_is_only_an_outage_if_it_lasts(tmp_path):
    """A co-resident harnessd taking the file lock must not flap readiness red.

    DuckDB grants one process at a time write access, and the single-machine
    setup in getting-started.md has harnessd sharing the hub's store -- so a
    momentary collision is routine (``bootstrap_harnessd_schema`` already
    treats it that way) and a readiness endpoint that screamed about it would
    be ignored by the time it mattered. A lock that never clears *is* an
    outage, so it goes red once it has persisted.

    A real second process, because the classification is on DuckDB's own
    wording for the conflict.
    """
    duckdb_path = _db(tmp_path)
    script = (
        "import sys, time, duckdb\n"
        "con = duckdb.connect(sys.argv[1])\n"
        "print('held', flush=True)\n"
        "time.sleep(60)\n"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", script, str(control_plane_path(duckdb_path))],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"

        forgiving = ReadinessProbe(
            duckdb_path, cache_seconds=0.0, busy_grace_seconds=30.0
        ).check()
        impatient = ReadinessProbe(
            duckdb_path, cache_seconds=0.0, busy_grace_seconds=0.0
        ).check()
    finally:
        holder.terminate()
        holder.wait(timeout=10)

    states = {store.store: store.state for store in forgiving.stores}
    assert states[STORE_CONTROL_PLANE] == STATE_BUSY, forgiving.as_dict()
    assert forgiving.ok
    assert {store.store: store.state for store in impatient.stores}[
        STORE_CONTROL_PLANE
    ] == STATE_FAILED
    assert not impatient.ok


def test_a_lakehouse_nothing_can_open_fails_readiness(tmp_path):
    """A store too broken to connect to leaves no handle to borrow.

    Without the openers reporting, that state is indistinguishable from an
    idle hub -- which would be #175 all over again, one layer down. Genuinely
    corrupt bytes here, so the error is DuckDB's own.
    """
    duckdb_path = _db(tmp_path)
    duckdb_path.write_bytes(b"not a duckdb file" * 512)
    with pytest.raises(duckdb.Error):
        open_duckdb_connection(duckdb_path, role="diagnostic")

    server, port = _serve(duckdb_path)
    try:
        status, body = _get(port, "/readyz")
    finally:
        server.shutdown()

    assert status == 503
    assert _states(body)[STORE_ANALYTICAL] == STATE_FAILED
    assert "valid DuckDB database file" in json.dumps(json.loads(body))


def test_an_open_that_failed_long_ago_is_not_held_against_the_store(tmp_path):
    """Evidence expires. A transient the hub recovered from is not an outage."""
    duckdb_path = _db(tmp_path)
    duckdb_path.write_bytes(b"not a duckdb file" * 512)
    with pytest.raises(duckdb.Error):
        open_duckdb_connection(duckdb_path, role="diagnostic")

    report = ReadinessProbe(
        duckdb_path, cache_seconds=0.0, connect_failure_window=0.0
    ).check()

    assert report.ok, report.as_dict()
    assert {store.store: store.state for store in report.stores}[
        STORE_ANALYTICAL
    ] == STATE_IDLE


def test_the_handle_register_does_not_grow_without_bound(tmp_path):
    """Every metrics refresh opens a snapshot copy under a new temporary path.

    The weak sets empty themselves; their keys would otherwise accumulate one
    per refresh for the life of the process.
    """
    from drover.server.db import _LIVE_CONNECTIONS, _LIVE_PATHS_SOFT_MAX

    duckdb_path = _db(tmp_path)
    for index in range(_LIVE_PATHS_SOFT_MAX + 10):
        snapshot = tmp_path / f"snapshot-{index}.duckdb"
        open_duckdb_connection(snapshot, role="diagnostic").close()

    assert len(_LIVE_CONNECTIONS) <= _LIVE_PATHS_SOFT_MAX + 1
    con = open_duckdb_connection(duckdb_path, role="diagnostic")
    try:
        assert live_connection(duckdb_path) is not None
    finally:
        con.close()


def test_a_hot_poller_is_served_from_a_brief_cache(tmp_path):
    """``/readyz`` may be polled hard; the probe must not amplify that."""
    duckdb_path = _db(tmp_path)
    probe = ReadinessProbe(duckdb_path, cache_seconds=30.0)

    first = probe.check()
    second = probe.check()

    assert first.checked_at == second.checked_at


def test_a_failure_is_never_served_from_the_cache(tmp_path):
    """A newly invalidated handle has to surface on the next poll, not later.

    Caching failures would keep a recovered hub red for the whole TTL and --
    worse -- keep serving a stale verdict while the state moves underneath it.
    """
    duckdb_path = _db(tmp_path)
    invalidated = _InvalidatedConnection()
    remember_live_connection(duckdb_path, invalidated)
    probe = ReadinessProbe(duckdb_path, cache_seconds=30.0)

    first = probe.check()
    second = probe.check()

    assert not first.ok
    assert first.checked_at != second.checked_at
