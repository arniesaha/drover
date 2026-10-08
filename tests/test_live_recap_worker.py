"""Tests for the live-session recap worker on the PostgreSQL job ledger (#480)."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from drover.schema import bootstrap
from drover.server.db import control_plane_connection
from drover.server.harness.recap_worker import LiveRecapWorker
from drover.server.harness.registry import HarnessRegistry
from drover.server.ledger import POLICIES, RECAP_SESSION, JobLedger, transaction
from drover.server.memory_store import MemoryRepository
from drover.server.summarizer.backends import BackendError


def recap_store(
    path: Path, *, session_id: str = "s1", completion_seq: int = 8
) -> HarnessRegistry:
    """A structured session with one content event and one turn completion.

    The completion is what enqueues the ``recap_session`` job, exactly as in
    production: the registry enqueues in the event's own transaction.
    """
    registry = HarnessRegistry(path)
    registry.create_session(
        session_id=session_id,
        host_id="laptop",
        harness="codex",
        command="codex",
        mode="structured",
    )
    registry.append_event(
        session_id=session_id,
        event_type="user_input",
        payload={"text": "Fix the recap cards."},
        content_preview="Fix the recap cards.",
        seq=completion_seq - 1,
    )
    complete(registry, session_id, completion_seq)
    return registry


def complete(registry: HarnessRegistry, session_id: str, seq: int) -> None:
    registry.append_event(
        session_id=session_id,
        event_type="status",
        payload={"turn_complete": True},
        seq=seq,
    )


def recap_row(path: Path, session_id: str) -> tuple[object, ...] | None:
    with control_plane_connection(path) as con:
        return con.execute(
            """SELECT recap_text, recap_source_seq, recap_model, phase
                 FROM session_memory WHERE session_id = ?""",
            [session_id],
        ).fetchone()


def jobs(path: Path, session_id: str) -> list[tuple[object, ...]]:
    """(source_version, status, failures, error_category), oldest first."""
    with control_plane_connection(path) as con:
        return con.execute(
            """SELECT source_version, status, failures, error_category
                 FROM pipeline_jobs WHERE job_kind = ? AND subject_key = ?
                ORDER BY enqueued_at, updated_at""",
            [RECAP_SESSION, session_id],
        ).fetchall()


def make_due(path: Path) -> None:
    with control_plane_connection(path) as con:
        con.execute(
            """UPDATE pipeline_jobs SET next_run_at = now() - interval '1 second'
                WHERE job_kind = ? AND status IN ('pending', 'retry_wait')""",
            [RECAP_SESSION],
        )


class StubBackend:
    name = "stub"
    model = "stub-recap-v1"

    def __init__(self, result: dict[str, object]) -> None:
        self.result = result
        self.calls = 0

    def summarize(self, prompt: str) -> dict:
        self.calls += 1
        return self.result


class FailingBackend:
    name = "failing"
    model = "failing-recap-v1"

    def __init__(self, message: str) -> None:
        self.message = message

    def summarize(self, prompt: str) -> dict:
        raise BackendError(self.message)


class BlockingBackend:
    name = "blocking"
    model = "blocking-recap-v1"

    def __init__(self) -> None:
        self._called = threading.Event()
        self._release = threading.Event()
        self._result: dict[str, object] | None = None

    def summarize(self, prompt: str) -> dict:
        self._called.set()
        assert self._release.wait(timeout=5)
        assert self._result is not None
        return self._result

    def wait_until_called(self) -> None:
        assert self._called.wait(timeout=5)

    def release(self, result: dict[str, object]) -> None:
        self._result = result
        self._release.set()


def test_worker_writes_the_live_phase_and_completes_the_job(
    pg_control_path: Path,
) -> None:
    recap_store(pg_control_path)
    backend = StubBackend({"recap": "**Fix cards** and verify snapshots."})

    assert (
        LiveRecapWorker(duckdb_path=pg_control_path, backend=backend).drain_once() == 1
    )

    assert recap_row(pg_control_path, "s1") == (
        "Fix cards and verify snapshots.",
        8,
        "stub-recap-v1",
        "live",
    )
    assert jobs(pg_control_path, "s1") == [("8", "succeeded", 0, None)]
    # The single latest-memory reader shows the live phase.
    latest = MemoryRepository(pg_control_path).latest(["s1"])["s1"]
    assert latest.phase == "live" and latest.text == "Fix cards and verify snapshots."
    # Nothing left to do: the next poll is idle and never calls the model.
    assert (
        LiveRecapWorker(duckdb_path=pg_control_path, backend=backend).drain_once() == 0
    )
    assert backend.calls == 1


def test_superseded_generation_discards_its_stale_recap(pg_control_path: Path) -> None:
    """A newer completion while a recap generates fences the older result out."""
    registry = recap_store(pg_control_path)
    backend = BlockingBackend()
    thread = threading.Thread(
        target=LiveRecapWorker(duckdb_path=pg_control_path, backend=backend).drain_once
    )
    thread.start()
    backend.wait_until_called()
    complete(registry, "s1", 10)
    backend.release({"recap": "Stale source eight recap."})
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert recap_row(pg_control_path, "s1") is None
    assert [row[:2] for row in jobs(pg_control_path, "s1")] == [
        ("8", "superseded"),
        ("10", "pending"),
    ]

    fresh = StubBackend({"recap": "Fresh source ten recap."})
    assert LiveRecapWorker(duckdb_path=pg_control_path, backend=fresh).drain_once() == 1
    assert recap_row(pg_control_path, "s1")[:2] == ("Fresh source ten recap.", 10)


def test_failures_retry_then_dead_letter_and_keep_the_previous_recap(
    pg_control_path: Path,
) -> None:
    recap_store(pg_control_path, completion_seq=8)
    assert (
        LiveRecapWorker(
            duckdb_path=pg_control_path,
            backend=StubBackend({"recap": "Existing recap."}),
        ).drain_once()
        == 1
    )
    complete(HarnessRegistry(pg_control_path), "s1", 10)
    worker = LiveRecapWorker(
        duckdb_path=pg_control_path, backend=FailingBackend("offline")
    )

    assert worker.drain_once() == 1
    assert jobs(pg_control_path, "s1")[-1] == ("10", "retry_wait", 1, "backend_error")
    assert recap_row(pg_control_path, "s1")[:2] == ("Existing recap.", 8)
    # Backoff: a retry_wait job is not due yet, so the next poll is idle.
    assert worker.drain_once() == 0

    max_attempts = POLICIES[RECAP_SESSION].max_attempts
    for _ in range(max_attempts - 1):
        make_due(pg_control_path)
        assert worker.drain_once() == 1

    assert jobs(pg_control_path, "s1")[-1][:3] == ("10", "dead_lettered", max_attempts)
    make_due(pg_control_path)
    assert worker.drain_once() == 0
    assert recap_row(pg_control_path, "s1")[:2] == ("Existing recap.", 8)


def test_missing_backend_releases_without_spending_an_attempt(
    pg_control_path: Path,
) -> None:
    recap_store(pg_control_path)

    assert LiveRecapWorker(duckdb_path=pg_control_path).drain_once() == 1

    assert jobs(pg_control_path, "s1") == [("8", "retry_wait", 0, "released")]
    assert recap_row(pg_control_path, "s1") is None


def test_new_worker_recovers_an_expired_lease(pg_control_path: Path) -> None:
    """A crashed worker's lease expires, counts as a failure, and is retried."""
    recap_store(pg_control_path)
    [job] = JobLedger(pg_control_path).claim(RECAP_SESSION, worker_id="crashed")
    with control_plane_connection(pg_control_path) as con:
        con.execute(
            "UPDATE pipeline_jobs SET lease_expires_at = now() - interval '1 second' "
            "WHERE job_id = ?",
            [job.job_id],
        )
    worker = LiveRecapWorker(
        duckdb_path=pg_control_path, backend=StubBackend({"recap": "Recovered recap."})
    )

    worker.drain_once()
    assert jobs(pg_control_path, "s1") == [("8", "retry_wait", 1, "lease_expired")]
    make_due(pg_control_path)
    assert worker.drain_once() == 1

    assert recap_row(pg_control_path, "s1")[:2] == ("Recovered recap.", 8)
    assert jobs(pg_control_path, "s1")[0][:2] == ("8", "succeeded")
    # The crashed owner's late completion cannot land.
    assert JobLedger(pg_control_path).complete(job) is False


def test_concurrent_workers_generate_one_recap_per_generation(
    pg_control_path: Path,
) -> None:
    """SKIP LOCKED claims: two workers never both take one job."""
    recap_store(pg_control_path)
    backends = [StubBackend({"recap": "First."}), StubBackend({"recap": "Second."})]
    barrier = threading.Barrier(2)
    handled: list[int] = []

    def drain(backend: StubBackend) -> None:
        barrier.wait(timeout=5)
        handled.append(
            LiveRecapWorker(duckdb_path=pg_control_path, backend=backend).drain_once()
        )

    threads = [threading.Thread(target=drain, args=(b,)) for b in backends]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert sorted(handled) == [0, 1]
    assert sum(b.calls for b in backends) == 1
    assert jobs(pg_control_path, "s1") == [("8", "succeeded", 0, None)]


def test_completion_and_recap_commit_together(pg_control_path: Path) -> None:
    """A recap write failure rolls the job transition back with it."""
    recap_store(pg_control_path)
    ledger = JobLedger(pg_control_path)
    [job] = ledger.claim(RECAP_SESSION, worker_id="w")
    with pytest.raises(Exception, match="division by zero"):
        with control_plane_connection(pg_control_path) as con:
            with transaction(con):
                assert ledger.complete(job, con=con) is True
                con.execute("SELECT 1/0")
    assert jobs(pg_control_path, "s1")[0][:2] == ("8", "running")


def test_duckdb_control_plane_drain_is_a_noop(tmp_path: Path) -> None:
    """Without PostgreSQL there is no memory store; the worker must not crash."""
    duckdb_path = tmp_path / "recaps.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    registry = HarnessRegistry(duckdb_path)
    registry.create_session(
        session_id="s1",
        host_id="laptop",
        harness="codex",
        command="codex",
        mode="structured",
    )
    complete(registry, "s1", 8)
    backend = StubBackend({"recap": "Never generated."})

    assert LiveRecapWorker(duckdb_path=duckdb_path, backend=backend).drain_once() == 0
    assert backend.calls == 0
    assert registry.latest_live_recaps(["s1"]) == {}


class LoopClock:
    """Run the polling loop without wall-clock sleeps."""

    def __init__(self, passes: int) -> None:
        self.passes = passes
        self.waits: list[float] = []

    def is_set(self) -> bool:
        return len(self.waits) >= self.passes

    def wait(self, interval: float) -> None:
        self.waits.append(interval)


def test_idle_loop_backs_off_and_work_restores_active_cadence(tmp_path, monkeypatch):
    worker = LiveRecapWorker(duckdb_path=tmp_path / "recap.duckdb")
    clock = LoopClock(8)
    worker._stop = clock
    # A job arriving at the slow cap is picked up on the next pass. Once
    # busy, the worker uses the original one-second cadence for every job.
    outcomes = iter([0, 0, 0, 1, 1, 1, 0, 0])
    monkeypatch.setattr(worker, "drain_once", lambda: next(outcomes))
    worker._loop()
    assert clock.waits == [2, 4, 5, 1, 1, 1, 2, 4]


def test_idle_loop_caps_poll_rate(tmp_path, monkeypatch):
    worker = LiveRecapWorker(duckdb_path=tmp_path / "recap.duckdb")
    clock = LoopClock(20)
    worker._stop = clock
    monkeypatch.setattr(worker, "drain_once", lambda: 0)
    worker._loop()
    assert clock.waits[:2] == [2, 4]
    assert clock.waits[2:] == [5] * 18
    assert sum(clock.waits) == 96  # Twenty polls instead of 96 at 1 Hz.


def test_idle_reconciliation_is_throttled(tmp_path, monkeypatch):
    worker = LiveRecapWorker(duckdb_path=tmp_path / "recap.duckdb")
    reconciled = []
    monkeypatch.setattr(
        HarnessRegistry,
        "reconcile_orphan_completions",
        lambda self: reconciled.append(True),
    )
    monkeypatch.setattr(
        "drover.server.harness.recap_worker.memory_store_available", lambda path: False
    )
    now = [0.0]
    monkeypatch.setattr(
        "drover.server.harness.recap_worker.time.monotonic", lambda: now[0]
    )
    for second in range(11):
        now[0] = float(second)
        assert worker.drain_once() == 0
    assert len(reconciled) == 3


def test_loop_errors_back_off_and_recover(tmp_path, monkeypatch):
    worker = LiveRecapWorker(duckdb_path=tmp_path / "recap.duckdb")
    clock = LoopClock(3)
    worker._stop = clock
    outcomes = iter([RuntimeError("offline"), 0, 1])

    def drain():
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(worker, "drain_once", drain)
    worker._loop()
    assert clock.waits == [2, 4, 1]


def test_job_arriving_at_idle_cap_is_claimed_on_next_poll(pg_control_path, monkeypatch):
    backend = StubBackend({"recap": "Picked up after idle."})
    worker = LiveRecapWorker(duckdb_path=pg_control_path, backend=backend)

    class ArrivalClock(LoopClock):
        def wait(self, interval):
            super().wait(interval)
            if len(self.waits) == 3:
                recap_store(pg_control_path)

    clock = ArrivalClock(5)
    worker._stop = clock
    monkeypatch.setattr(
        "drover.server.harness.recap_worker.time.monotonic", lambda: sum(clock.waits)
    )
    worker._loop()
    assert clock.waits == [2, 4, 5, 1, 2]
    assert backend.calls == 1
    assert recap_row(pg_control_path, "s1")[:2] == ("Picked up after idle.", 8)
    assert jobs(pg_control_path, "s1") == [("8", "succeeded", 0, None)]
