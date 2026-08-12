"""The control plane's read path must not share anything with analytics.

Issue #95. Three separate wedges with three separate causes produced one
symptom: ``/healthz`` answering in under a millisecond while every
``/harness*`` endpoint timed out at 20-40s. Each fix removed one analytical
contributor and the symptom came back through the next one, because the
control plane and every background scanner shared a single DuckDB instance
*and* a single process-wide connect lock.

These tests pin the structural invariant rather than any one contributor:
whatever an analytical reader does, it cannot reach the control plane's
connection or its lock, in either direction. They are the enforcement for
the eighth contributor nobody has identified yet.
"""

from __future__ import annotations

import threading
import time

import duckdb
import pytest

from drover.schema import bootstrap
from drover.server.db import (
    ROLE_DEFAULTS,
    close_control_plane_connections,
    control_plane_connection,
    control_plane_lock,
    duckdb_connect_lock,
    open_duckdb_connection,
    pin_control_plane_connection,
)
from drover.server.harness.registry import HarnessRegistry

# Every assertion about "does not block" needs a ceiling. The control-plane
# reads under test are indexed lookups measured in single-digit milliseconds;
# a second is three orders of magnitude of headroom, so a failure here means
# the operation blocked, not that the machine was busy.
UNBLOCKED_SECONDS = 1.0


def _db(tmp_path):
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    return duckdb_path


def _run_with_deadline(fn, *, timeout: float = UNBLOCKED_SECONDS):
    """Run ``fn`` on a worker thread and fail if it does not finish in time.

    A plain call would hang the whole test session when the invariant breaks,
    which reads as an infrastructure problem instead of a failure.
    """
    box: dict[str, object] = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - reported on the main thread
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout=timeout)
    if thread.is_alive():
        pytest.fail(f"blocked for more than {timeout}s")
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box.get("value")


@pytest.fixture(autouse=True)
def _release_pins(monkeypatch):
    """Exercise the pinned path, and never leak a pin into the next test.

    Pinning is opt-in in production (see
    ``test_pinning_is_off_unless_an_operator_asks_for_it``), but it is the
    mechanism most of these tests are about, so it is switched on here. The
    two tests that assert the default override this with ``monkeypatch``.
    """
    monkeypatch.setenv("DROVER_CONTROL_PLANE_PIN", "1")
    yield
    close_control_plane_connections()


def test_the_control_plane_lock_is_never_the_analytical_connect_lock(tmp_path):
    """The two lock tables must stay separate objects for the same path.

    Sharing them is the mechanism from #95: ``HarnessRegistry._connect`` held
    the process-wide connect lock for its whole window, so a listing request
    queued behind whichever analytical connect happened to be in flight.
    """
    duckdb_path = _db(tmp_path)

    assert control_plane_lock(duckdb_path) is not duckdb_connect_lock(duckdb_path)
    # Same path twice is the same lock -- the isolation is between roles, not
    # between calls, or control-plane windows would stop serializing.
    assert control_plane_lock(duckdb_path) is control_plane_lock(duckdb_path)


def test_a_stuck_analytical_connect_cannot_block_a_control_plane_read(tmp_path):
    """A control-plane read must survive an analytical connect that never returns.

    Holding ``duckdb_connect_lock`` is exactly the process state during the
    live wedges: ``sample(1)`` caught threads parked inside
    ``DuckDBPyConnection::Connect`` on a saturated instance, and every
    ``/harness*`` request was queued behind them on this lock.
    """
    duckdb_path = _db(tmp_path)
    pin_control_plane_connection(duckdb_path)

    def read() -> int:
        with control_plane_connection(duckdb_path) as con:
            return con.execute("SELECT count(*) FROM harness_hosts").fetchone()[0]

    with duckdb_connect_lock(duckdb_path):
        assert _run_with_deadline(read) == 0


def test_a_control_plane_lock_holder_cannot_block_an_analytical_connect(tmp_path):
    """...and the isolation has to hold in the other direction too.

    Otherwise the fix would simply move the queue: a slow control-plane
    window would start stalling ingest and the workers behind it.
    """
    duckdb_path = _db(tmp_path)
    pin_control_plane_connection(duckdb_path)

    def analytical_read() -> int:
        # `tasks`, not `harness_hosts`: since #95 the control-plane tables are
        # not in this database at all, which is the stronger form of the same
        # isolation this test is about.
        con = open_duckdb_connection(duckdb_path, role="diagnostic")
        try:
            return con.execute("SELECT count(*) FROM tasks").fetchone()[0]
        finally:
            con.close()

    with control_plane_lock(duckdb_path):
        assert _run_with_deadline(analytical_read) == 0


def test_a_pinned_control_plane_never_opens_another_connection(tmp_path, monkeypatch):
    """Once pinned, control-plane reads must not call ``duckdb.connect`` again.

    The connect is the part that blocks on a saturated instance, and the
    registry paid one per window -- three of them for a single ``/harness``
    render. Pinning is what makes the separate lock safe: with no further
    connects there is no instance-cache race left to serialize.
    """
    duckdb_path = _db(tmp_path)
    pin_control_plane_connection(duckdb_path)

    def explode(*args, **kwargs):
        raise AssertionError("control-plane read opened a new DuckDB connection")

    monkeypatch.setattr(duckdb, "connect", explode)

    with control_plane_connection(duckdb_path) as con:
        assert con.execute("SELECT count(*) FROM harness_sessions").fetchone()[0] == 0


@pytest.mark.parametrize("role", sorted(ROLE_DEFAULTS))
def test_analytical_roles_never_receive_the_control_plane_connection(tmp_path, role):
    """No analytical role may be handed the control plane's connection.

    Sharing the object would re-couple them: DuckDB serializes statements on
    one connection, so a 20s scan would sit directly in front of every fleet
    listing.
    """
    duckdb_path = _db(tmp_path)
    pin_control_plane_connection(duckdb_path)
    with control_plane_connection(duckdb_path) as pinned:
        control_plane_con = pinned

    con = open_duckdb_connection(duckdb_path, role=role)
    try:
        assert con is not control_plane_con
    finally:
        con.close()


def test_a_broken_control_plane_connection_is_replaced_on_the_next_read(tmp_path):
    """A pin that dies must not wedge the control plane until a restart.

    Restarting ``com.drover.server`` is the mitigation this issue exists to
    remove; a permanently dead pin would just reintroduce it. The window that
    hits the dead connection still fails -- every control-plane caller
    already logs and moves on -- and the one after it recovers.
    """
    duckdb_path = _db(tmp_path)
    pin_control_plane_connection(duckdb_path)

    with control_plane_connection(duckdb_path) as con:
        first = con
    first.close()  # simulate the pin dying underneath us

    with pytest.raises(duckdb.Error):
        with control_plane_connection(duckdb_path) as con:
            con.execute("SELECT count(*) FROM harness_hosts").fetchone()

    with control_plane_connection(duckdb_path) as con:
        assert con is not first
        assert con.execute("SELECT count(*) FROM harness_hosts").fetchone()[0] == 0


def test_an_ordinary_query_error_keeps_the_pin(tmp_path):
    """Only a broken *connection* may cost the pin, never a broken statement.

    ``create_session`` and ``append_event`` both insert on a primary key and
    raise ConstraintException on a replay, which is routine. Dropping the pin
    for those would send the next window back through the shared connect lock
    -- reintroducing exactly the coupling this removes, on the hub's most
    common recoverable error.
    """
    duckdb_path = _db(tmp_path)
    pin_control_plane_connection(duckdb_path)
    with control_plane_connection(duckdb_path) as con:
        pinned = con

    registry = HarnessRegistry(duckdb_path)
    registry.register_host(host_id="hub", display_name="Hub", kind="darwin")
    registry.create_session(
        host_id="hub", harness="shell", command="bash", session_id="dup"
    )
    with pytest.raises(duckdb.ConstraintException):
        registry.create_session(
            host_id="hub", harness="shell", command="bash", session_id="dup"
        )

    with control_plane_connection(duckdb_path) as con:
        assert con is pinned


def test_without_a_pin_the_control_plane_still_serves(tmp_path):
    """Pinning is opt-in, and its absence must be a degradation, not an outage.

    Only the hub server pins: DuckDB gives one process at a time write access
    to a database file, so a pin held by harnessd would lock the server out of
    its own store (and vice versa). Everywhere else the control plane keeps
    the pre-#95 connect-per-window path.
    """
    duckdb_path = _db(tmp_path)

    with control_plane_connection(duckdb_path) as con:
        assert con.execute("SELECT count(*) FROM harness_hosts").fetchone()[0] == 0


def test_pinning_reports_failure_instead_of_raising(tmp_path):
    """A database another process already owns must not stop the server booting.

    ``bootstrap_harnessd_schema`` already treats a conflicting lock as a
    degraded mode rather than a fatal one; pinning follows that precedent.
    """
    duckdb_path = tmp_path / "nonexistent" / "drover.duckdb"

    assert pin_control_plane_connection(duckdb_path) is False


def test_pinning_is_off_unless_an_operator_asks_for_it(tmp_path, monkeypatch):
    """The default must not take DuckDB's file lock away from harnessd.

    A pin holds the exclusive file lock for the life of the process. On a host
    where the hub server and ``drover-harnessd`` share ``cfg.duckdb_path``
    -- the single-machine setup in getting-started.md -- that would take
    harnessd's registry permanently dark, and harnessd serves its fleet
    endpoints from that registry. ``bootstrap_harnessd_schema`` already treats
    "Could not set lock" as routine, which is the evidence they collide today
    and survive only because the server's windows are short.

    So the lock split ships on by default and the pin is opt-in.
    """
    monkeypatch.delenv("DROVER_CONTROL_PLANE_PIN", raising=False)
    duckdb_path = _db(tmp_path)

    assert pin_control_plane_connection(duckdb_path) is False

    real_connect = duckdb.connect
    seen: list[str] = []
    monkeypatch.setattr(
        duckdb,
        "connect",
        lambda *a, **k: (seen.append("connect"), real_connect(*a, **k))[1],
    )
    with control_plane_connection(duckdb_path) as con:
        con.execute("SELECT 1").fetchone()

    assert seen, "with pinning off every window opens its own connection"


def test_pinning_turns_on_for_a_server_that_owns_the_file(tmp_path, monkeypatch):
    """Opt-in has to actually opt in, on a host where nothing else shares it."""
    monkeypatch.setenv("DROVER_CONTROL_PLANE_PIN", "1")
    duckdb_path = _db(tmp_path)

    assert pin_control_plane_connection(duckdb_path) is True

    real_connect = duckdb.connect
    seen: list[str] = []
    monkeypatch.setattr(
        duckdb,
        "connect",
        lambda *a, **k: (seen.append("connect"), real_connect(*a, **k))[1],
    )
    with control_plane_connection(duckdb_path) as con:
        con.execute("SELECT 1").fetchone()

    assert not seen, "a pinned window must not open a connection"


def test_the_registry_reads_through_the_control_plane_connection(tmp_path):
    """The registry is the control plane, so it has to be on this path.

    ``/harness``, ``/harness/hosts`` and ``/harness/sessions`` are all
    ``HarnessRegistry`` calls; ``session_messages`` stayed at ~1ms through
    every wedge precisely because it was not.
    """
    duckdb_path = _db(tmp_path)
    pin_control_plane_connection(duckdb_path)
    registry = HarnessRegistry(duckdb_path)
    registry.register_host(host_id="hub", display_name="Hub", kind="darwin")

    with duckdb_connect_lock(duckdb_path):
        hosts = _run_with_deadline(registry.list_hosts)

    assert [host.host_id for host in hosts] == ["hub"]


def test_a_stale_pin_is_dropped_when_the_connection_is_closed(tmp_path):
    """``close_control_plane_connections`` has to actually release the file.

    The server calls it on shutdown; without it a restart would race its own
    previous process for the database lock.
    """
    duckdb_path = _db(tmp_path)
    assert pin_control_plane_connection(duckdb_path) is True

    close_control_plane_connections()

    seen: list[str] = []
    real_connect = duckdb.connect

    def recording_connect(*args, **kwargs):
        seen.append("connect")
        return real_connect(*args, **kwargs)

    duckdb.connect = recording_connect
    try:
        with control_plane_connection(duckdb_path) as con:
            con.execute("SELECT 1").fetchone()
    finally:
        duckdb.connect = real_connect

    assert seen, "a released pin must fall back to opening a connection"


def test_control_plane_windows_serialize_against_each_other(tmp_path):
    """One connection, so two threads must not execute on it at once.

    DuckDB's Python connection is not safe for concurrent use; the control
    plane's own lock is what replaces the process-wide one it used to hold.
    """
    duckdb_path = _db(tmp_path)
    pin_control_plane_connection(duckdb_path)
    overlaps = []
    inside = threading.Event()

    def slow_window() -> None:
        with control_plane_connection(duckdb_path):
            inside.set()
            time.sleep(0.2)

    def second_window() -> None:
        inside.wait(UNBLOCKED_SECONDS)
        started = time.monotonic()
        with control_plane_connection(duckdb_path):
            overlaps.append(time.monotonic() - started)

    threads = [
        threading.Thread(target=slow_window),
        threading.Thread(target=second_window),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert overlaps and overlaps[0] >= 0.1
