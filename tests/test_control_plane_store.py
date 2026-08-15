"""The control plane owns a database *file*, not just a connection.

Issue #95, second cut. PR #104 gave ``/harness*`` its own lock and, optionally,
its own connection. It was deployed on ``c7900e7`` and the wedge happened again
anyway: ``/healthz`` in 0.49ms while ``/harness`` and ``/harness/hosts`` timed
out at 25s and then 60s, with ``sample(1)`` parked in ``ParquetReader``/``zstd``
and the process in uninterruptible wait.

The reason a lock split could not fix it is that **a separate connection is not
a separate instance**. Every connection to one path in one process joins one
cached DuckDB instance: one scheduler, one buffer manager, and one
``memory_limit``. Both of these were logged on the live hub the same night::

    cockpit:  Out of Memory Error: failed to allocate 16.0 MiB (1.8 GiB/1.8 GiB used)
    advisory: OutOfMemoryException: failed to pin block of size 4.0 KiB (1.8 GiB/1.8 GiB used)

That 1.8 GiB is ``ROLE_DEFAULTS[...]["memory_limit"] = "2GB"``, and it was
shared by the control plane and every analytical worker.

The control-plane state is tiny -- 3 hosts, 120 sessions, 41,649 events --
and it was living inside a 640.8 MB analytical store. These tests pin the
split: its own file, its own instance, its own budget, and a *copy* for the
analytical queries that genuinely join across both worlds.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import duckdb
import pytest

from drover.schema import bootstrap
from drover.server.db import (
    CONTROL_PLANE_TABLES,
    ROLE_DEFAULTS,
    ControlPlaneBusy,
    attached_control_plane_snapshot,
    close_control_plane_connections,
    control_plane_connection,
    control_plane_lock,
    control_plane_path,
    control_plane_snapshot,
    open_duckdb_connection,
    pin_control_plane_connection,
)
from drover.server.harness.registry import HarnessRegistry


@pytest.fixture(autouse=True)
def _release_pins():
    yield
    close_control_plane_connections()


def _db(tmp_path: Path) -> Path:
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    return duckdb_path


def test_the_control_plane_lives_in_its_own_database_file(tmp_path):
    """Its own file is the only thing that gives it its own DuckDB instance.

    41,649 registry rows were living inside a 640.8 MB analytical store purely
    because that is where they were first written.
    """
    duckdb_path = _db(tmp_path)
    registry_path = control_plane_path(duckdb_path)

    assert registry_path != duckdb_path
    assert registry_path.exists()

    con = duckdb.connect(str(registry_path))
    try:
        tables = {
            row[0]
            for row in con.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_type = 'BASE TABLE'"
            ).fetchall()
        }
    finally:
        con.close()
    assert set(CONTROL_PLANE_TABLES) <= tables


def test_resolving_the_control_plane_path_is_idempotent(tmp_path):
    """Handing the registry its own path back must not nest a second suffix."""
    duckdb_path = tmp_path / "drover.duckdb"
    once = control_plane_path(duckdb_path)

    assert control_plane_path(once) == once


def test_an_operator_can_place_the_control_plane_store_anywhere(tmp_path, monkeypatch):
    """The default sits beside the analytical store; the location is a choice."""
    elsewhere = tmp_path / "somewhere" / "control-plane.duckdb"
    monkeypatch.setenv("DROVER_CONTROL_PLANE_DUCKDB", str(elsewhere))

    assert control_plane_path(tmp_path / "drover.duckdb") == elsewhere


def test_the_control_plane_never_opens_the_analytical_database(tmp_path):
    """A registry window must not touch the file the scans are reading.

    This is the whole fix in one assertion. While it opened ``drover.duckdb``
    it joined the analytical instance no matter which lock it held or whether
    its connection was pinned.
    """
    duckdb_path = _db(tmp_path)
    opened: list[str] = []
    real_connect = duckdb.connect

    def recording_connect(database=":memory:", *args, **kwargs):
        opened.append(str(database))
        return real_connect(database, *args, **kwargs)

    duckdb.connect = recording_connect
    try:
        registry = HarnessRegistry(duckdb_path)
        registry.register_host(host_id="hub", display_name="Hub", kind="darwin")
        registry.list_sessions()
    finally:
        duckdb.connect = real_connect

    assert opened, "the registry must have opened something"
    assert str(duckdb_path) not in opened


def test_an_analytical_memory_limit_cannot_reach_the_control_plane(
    tmp_path, monkeypatch
):
    """``memory_limit`` is instance-wide, so the split is what un-shares it.

    Both of the live OOMs on 2026-08-11 hit the same 1.8 GiB ceiling because
    the control plane and every analytical worker were one instance. Setting
    the analytical budget must now be invisible to the control plane.

    Pinned deliberately: an unpinned window reapplies the control-plane role on
    every open, which would hide a shared instance behind the reset rather than
    detect it. With one connection held, whatever the analytical side does to
    the instance is what this reads back.
    """
    monkeypatch.setenv("DROVER_CONTROL_PLANE_PIN", "1")
    duckdb_path = _db(tmp_path)
    assert pin_control_plane_connection(duckdb_path) is True

    with control_plane_connection(duckdb_path) as con:
        before = con.execute("SELECT current_setting('memory_limit')").fetchone()[0]

    analytical = open_duckdb_connection(duckdb_path, role="diagnostic")
    try:
        analytical.execute("SET memory_limit='47MB'")
        analytical_limit = analytical.execute(
            "SELECT current_setting('memory_limit')"
        ).fetchone()[0]
        with control_plane_connection(duckdb_path) as con:
            after = con.execute("SELECT current_setting('memory_limit')").fetchone()[0]
    finally:
        analytical.close()

    assert analytical_limit != before, "the analytical budget did not actually change"
    assert after == before


def test_the_control_plane_budget_cannot_reach_an_analytical_reader(tmp_path):
    """...and the isolation has to hold in the other direction too.

    A registry that could shrink the analytical budget would just move the OOM.
    """
    duckdb_path = _db(tmp_path)
    analytical = open_duckdb_connection(duckdb_path, role="diagnostic")
    try:
        before = analytical.execute(
            "SELECT current_setting('memory_limit')"
        ).fetchone()[0]

        with control_plane_connection(duckdb_path) as con:
            con.execute("SET memory_limit='53MB'")
            control_plane_limit = con.execute(
                "SELECT current_setting('memory_limit')"
            ).fetchone()[0]

        after = analytical.execute("SELECT current_setting('memory_limit')").fetchone()[
            0
        ]
    finally:
        analytical.close()

    assert control_plane_limit != before, "the control-plane budget did not change"
    assert after == before


def test_the_control_plane_runs_on_a_budget_sized_for_its_own_data(tmp_path):
    """A private instance defaults to 80% of host RAM unless we say otherwise.

    On a 16 GB laptop with ~6 GB free and 10.8M pageouts, letting a second
    instance claim ~12.7 GiB would trade one failure for a worse one.
    """
    duckdb_path = _db(tmp_path)

    with control_plane_connection(duckdb_path) as con:
        limit, threads = con.execute(
            "SELECT current_setting('memory_limit'), current_setting('threads')"
        ).fetchone()

    assert "MiB" in str(limit), f"control-plane memory_limit is {limit!r}"
    assert int(threads) == int(ROLE_DEFAULTS["control_plane"]["threads"])


def test_analytical_readers_reach_control_plane_state_through_a_private_copy(tmp_path):
    """The cross-world joins are real, so they need a supported way across.

    ``advisory/worker.py`` joins ``spans_enriched`` to ``harness_sessions`` and
    ``cockpit/analytics.py`` correlates ``harness_sessions`` with span sessions.
    DuckDB refuses to ``ATTACH`` a file another instance in the process already
    holds ("Unique file handle conflict"), so the copy is not a preference.
    """
    duckdb_path = _db(tmp_path)
    registry = HarnessRegistry(duckdb_path)
    registry.register_host(host_id="hub", display_name="Hub", kind="darwin")
    registry.create_session(
        host_id="hub", harness="claude", command="claude", session_id="s1"
    )

    con = open_duckdb_connection(duckdb_path, role="diagnostic")
    try:
        with attached_control_plane_snapshot(con, duckdb_path):
            rows = con.execute("SELECT session_id FROM harness_sessions").fetchall()
            attached = {
                row[0]
                for row in con.execute(
                    "SELECT path FROM duckdb_databases() WHERE path IS NOT NULL"
                ).fetchall()
            }
    finally:
        con.close()

    assert [row[0] for row in rows] == ["s1"]
    assert (
        str(control_plane_path(duckdb_path)) not in attached
    ), "an analytical reader must never attach the live control-plane file"


def test_an_unchanged_control_plane_store_is_copied_once_not_per_reader(tmp_path):
    """The copy has to be cheap at the rate the readers actually run.

    ``AdvisoryScheduler.enqueue_due_full_review`` calls
    ``operational_snapshot_source_version`` once per analyzer, and the advisory
    worker polls it every 5s: six snapshot windows every five seconds, forever.
    A fresh copy per window would put a continuous write load on a host that is
    already paging, which is precisely the cost that made generalising #76's
    copy-on-read the wrong answer.
    """
    duckdb_path = _db(tmp_path)
    HarnessRegistry(duckdb_path).register_host(
        host_id="hub", display_name="Hub", kind="darwin"
    )

    with control_plane_snapshot(duckdb_path) as first:
        first_id = first.stat().st_ino
    with control_plane_snapshot(duckdb_path) as second:
        second_id = second.stat().st_ino

    assert first_id == second_id


def test_a_changed_control_plane_store_is_copied_again(tmp_path):
    """Cheap must not mean stale: a new session has to show up in the next read."""
    duckdb_path = _db(tmp_path)
    registry = HarnessRegistry(duckdb_path)
    registry.register_host(host_id="hub", display_name="Hub", kind="darwin")

    with control_plane_snapshot(duckdb_path) as before:
        before_sessions = _sessions_in(before)

    registry.create_session(
        host_id="hub", harness="claude", command="claude", session_id="added"
    )

    with control_plane_snapshot(duckdb_path) as after:
        after_sessions = _sessions_in(after)

    assert before_sessions == []
    assert after_sessions == ["added"]


def test_a_reader_keeps_its_snapshot_when_another_reader_refreshes(tmp_path):
    """One reader's copy must not be swapped out from under its open ATTACH."""
    duckdb_path = _db(tmp_path)
    registry = HarnessRegistry(duckdb_path)
    registry.register_host(host_id="hub", display_name="Hub", kind="darwin")

    with control_plane_snapshot(duckdb_path) as held:
        registry.create_session(
            host_id="hub", harness="claude", command="claude", session_id="added"
        )
        with control_plane_snapshot(duckdb_path) as refreshed:
            assert _sessions_in(refreshed) == ["added"]
        assert _sessions_in(held) == []


def test_two_analytical_readers_can_hold_snapshots_at_the_same_time(tmp_path):
    """The cockpit and the advisory worker are separate threads on one instance.

    ``ATTACH`` is instance-wide, not per connection, so a fixed catalog alias
    would make the second reader fail with "database with name ... already
    exists" and its teardown would detach the first reader's snapshot mid-query.
    Attaching one shared copy twice fails the same way ("Unique file handle
    conflict"). Both readers have to work while the other is inside its window.
    """
    duckdb_path = _db(tmp_path)
    registry = HarnessRegistry(duckdb_path)
    registry.register_host(host_id="hub", display_name="Hub", kind="darwin")
    registry.create_session(
        host_id="hub", harness="claude", command="claude", session_id="s1"
    )

    first = open_duckdb_connection(duckdb_path, role="diagnostic")
    second = open_duckdb_connection(duckdb_path, role="diagnostic")
    try:
        with attached_control_plane_snapshot(first, duckdb_path):
            with attached_control_plane_snapshot(second, duckdb_path):
                inner = second.execute(
                    "SELECT session_id FROM harness_sessions"
                ).fetchall()
            outer = first.execute("SELECT session_id FROM harness_sessions").fetchall()
    finally:
        first.close()
        second.close()

    assert [row[0] for row in inner] == ["s1"]
    assert [row[0] for row in outer] == ["s1"]


def test_the_snapshot_includes_rows_a_writer_still_has_in_the_write_ahead_log(
    tmp_path, monkeypatch
):
    """Copying the database file alone silently loses everything in its WAL.

    Measured: with a connection held open, a fresh insert lives entirely in
    ``<db>.wal`` and a ``shutil.copy2`` of the database alone produces a copy
    where the table does not exist at all. DuckDB checkpoints on last close, so
    this is invisible whenever nothing holds the store -- and permanent under
    ``DROVER_CONTROL_PLANE_PIN=1``, which holds it for the life of the process.
    """
    monkeypatch.setenv("DROVER_CONTROL_PLANE_PIN", "1")
    duckdb_path = _db(tmp_path)
    assert pin_control_plane_connection(duckdb_path) is True
    registry = HarnessRegistry(duckdb_path)
    registry.register_host(host_id="hub", display_name="Hub", kind="darwin")
    registry.create_session(
        host_id="hub", harness="claude", command="claude", session_id="uncheckpointed"
    )

    with control_plane_snapshot(duckdb_path) as snapshot:
        assert _sessions_in(snapshot) == ["uncheckpointed"]


def _sessions_in(snapshot: Path) -> list[str]:
    con = duckdb.connect(str(snapshot))
    try:
        return [
            row[0]
            for row in con.execute(
                "SELECT session_id FROM harness_sessions ORDER BY session_id"
            ).fetchall()
        ]
    finally:
        con.close()


def test_the_snapshot_shadows_stale_control_plane_tables_left_behind(tmp_path):
    """Migration leaves the old tables in place, so they must not be readable.

    Rolling back a deploy has to be a restart, not a data-recovery exercise --
    but a stale ``harness_sessions`` still sitting in ``drover.duckdb`` would
    otherwise let an analytical query silently answer from it.
    """
    duckdb_path = _db(tmp_path)
    registry = HarnessRegistry(duckdb_path)
    registry.register_host(host_id="hub", display_name="Hub", kind="darwin")
    registry.create_session(
        host_id="hub", harness="claude", command="claude", session_id="fresh"
    )

    legacy = duckdb.connect(str(duckdb_path))
    try:
        legacy.execute(
            "CREATE TABLE IF NOT EXISTS harness_sessions "
            "(session_id VARCHAR, host_id VARCHAR)"
        )
        legacy.execute("INSERT INTO harness_sessions VALUES ('stale', 'hub')")
    finally:
        legacy.close()

    con = open_duckdb_connection(duckdb_path, role="diagnostic")
    try:
        with attached_control_plane_snapshot(con, duckdb_path):
            seen = [
                row[0]
                for row in con.execute(
                    "SELECT session_id FROM harness_sessions"
                ).fetchall()
            ]
    finally:
        con.close()

    assert seen == ["fresh"]


def test_the_advisory_snapshot_still_joins_spans_to_control_plane_sessions(tmp_path):
    """``advisory/worker.py`` joins ``spans_enriched`` to ``harness_sessions``.

    The two worlds do meet, and the join has to survive being split across two
    files or the advisory analyzers go blind.
    """
    from drover.server.advisory.worker import load_operational_snapshot

    duckdb_path = _db(tmp_path)
    registry = HarnessRegistry(duckdb_path)
    registry.register_host(host_id="mac-mini", display_name="Mac mini", kind="darwin")
    registry.create_session(
        host_id="mac-mini",
        harness="codex",
        command="codex",
        session_id="s1",
        repo_owner="acme",
        repo_name="drover",
        status="completed",
    )

    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute("DROP VIEW spans_enriched")
        con.execute("""
            CREATE TABLE spans_enriched (
              span_id VARCHAR, session_id VARCHAR, start_time TIMESTAMPTZ,
              llm_provider VARCHAR, routing_provider VARCHAR, routing_model VARCHAR,
              prompt_tokens BIGINT, total_tokens BIGINT,
              cache_read_tokens BIGINT, cost_usd DOUBLE)
        """)
        con.execute(
            "INSERT INTO spans_enriched VALUES ('span-1', 's1', now(), 'openai', "
            "'openai', 'gpt-4', 20000, 21000, 0, 1.5)"
        )
    finally:
        con.close()

    snapshot = load_operational_snapshot(
        duckdb_path, "deterministic.telemetry_coverage", "fleet", "facts:v1"
    )

    assert [item.total_sessions for item in snapshot.telemetry] == [1]


def test_the_cockpit_activity_query_still_sees_control_plane_sessions(tmp_path):
    """``cockpit/analytics.py`` correlates ``harness_sessions`` with span sessions.

    This is the query that was in flight during the 2026-08-11 19:45 wedge, so
    it is also the one most likely to be quietly broken by moving the table.
    """
    from drover.server.cockpit.analytics import AnalyticsFilters
    from drover.server.cockpit.service import CockpitService

    duckdb_path = _db(tmp_path)
    registry = HarnessRegistry(duckdb_path)
    registry.register_host(host_id="mac-mini", display_name="Mac mini", kind="darwin")
    registry.create_session(
        host_id="mac-mini", harness="codex", command="codex", session_id="s1"
    )

    service = CockpitService(duckdb_path=duckdb_path, provider_usage=None)
    activity = service.analytics(AnalyticsFilters(days=7))["activity"]

    assert activity["status"] == "ok", activity
    assert activity["data"]["totals"]["session_count"] == 1


def test_a_window_can_be_given_a_budget_instead_of_waiting(tmp_path):
    """Issue #181. A caller that must answer on a deadline needs a way out.

    ``/readyz`` is the caller: waiting indefinitely for a window turned a slow
    control-plane read into a permanently dark endpoint. The budget is opt-in,
    so every existing caller -- the registry, the recap worker, bootstrap --
    keeps waiting its turn exactly as before.
    """
    duckdb_path = _db(tmp_path)

    with control_plane_lock(duckdb_path):
        started = time.monotonic()
        with pytest.raises(ControlPlaneBusy):
            with control_plane_connection(duckdb_path, timeout=0.05) as con:
                con.execute("SELECT 1").fetchone()
        waited = time.monotonic() - started

    assert waited < 1.0, f"gave up after {waited:.2f}s, not the 0.05s budget"


def test_a_budget_is_only_spent_when_the_window_is_taken(tmp_path):
    """An uncontended window with a budget must behave like any other window."""
    duckdb_path = _db(tmp_path)

    with control_plane_connection(duckdb_path, timeout=0.05) as con:
        assert con.execute("SELECT 1").fetchone()[0] == 1

    # ...and the lock is released again, or the next window would inherit it.
    assert control_plane_lock(duckdb_path).acquire(timeout=0.5)
    control_plane_lock(duckdb_path).release()


def test_a_bounded_window_that_gives_up_holds_nothing(tmp_path):
    """A timed-out acquisition must not leave the lock held or half-held."""
    duckdb_path = _db(tmp_path)
    holder_inside = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with control_plane_lock(duckdb_path):
            holder_inside.set()
            release.wait(timeout=5)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert holder_inside.wait(timeout=5)
    with pytest.raises(ControlPlaneBusy):
        with control_plane_connection(duckdb_path, timeout=0.05):
            pass
    release.set()
    holder.join(timeout=5)

    with control_plane_connection(duckdb_path) as con:
        assert con.execute("SELECT 1").fetchone()[0] == 1


def test_the_snapshot_is_released_when_the_reader_is_done(tmp_path):
    """A leaked attachment would hold a temp copy open for the whole process."""
    duckdb_path = _db(tmp_path)

    con = open_duckdb_connection(duckdb_path, role="diagnostic")
    try:
        with attached_control_plane_snapshot(con, duckdb_path):
            attached = con.execute(
                "SELECT count(*) FROM duckdb_databases()"
            ).fetchone()[0]
        after = con.execute("SELECT count(*) FROM duckdb_databases()").fetchone()[0]
    finally:
        con.close()

    assert after < attached
