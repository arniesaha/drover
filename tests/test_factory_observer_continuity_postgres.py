"""Real PostgreSQL owner races; uses only the repository's disposable facility.

The pg_control_path -> postgres_dsn fixtures explicitly skip when neither
DROVER_TEST_POSTGRES_DSN nor local initdb exists (or psycopg is missing).
No DuckDB fallback and no production control-store DSN are used here.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest

from drover.server.control_store import (
    close_control_store,
    configure_control_store,
    control_store_config,
)
from drover.server.db import control_plane_connection
from drover.server.harness.continuity import (
    ContinuityConflict,
    FactoryObserverContinuity,
)
from drover.server.harness.registry import HarnessRegistry

RUN = "run_PG497"


@pytest.fixture(scope="session", autouse=True)
def _pg_driver_dependencies():
    pytest.importorskip(
        "psycopg",
        reason="PostgreSQL continuity proof requires psycopg (drover[postgres])",
    )
    pytest.importorskip(
        "psycopg_pool",
        reason="PostgreSQL continuity proof requires psycopg_pool (drover[postgres])",
    )


class Clock:
    now = datetime(2026, 10, 3, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def pg_bridge(pg_control_path):
    with control_plane_connection(pg_control_path) as con:
        assert con.dialect == "postgres"
        print(
            "Actual PostgreSQL server:", con.execute("SELECT version()").fetchone()[0]
        )
    registry = HarnessRegistry(pg_control_path)
    registry.create_session(
        session_id="pg-observer",
        host_id="studio",
        harness="codex",
        command="codex",
        status="running",
        handoff_mode="factory_observer",
        source_session_id=f"factory/{RUN}@4",
    )
    clock = Clock()
    store = FactoryObserverContinuity(pg_control_path, clock=clock)
    store.initialize(
        session_id="pg-observer",
        objective="Prove #497 on PostgreSQL",
        checkpoint="Pending",
    )
    return store, clock


def race(functions):
    barrier = Barrier(len(functions))

    def execute(function):
        barrier.wait(timeout=5)
        try:
            return function()
        except ContinuityConflict as exc:
            return exc

    with ThreadPoolExecutor(max_workers=len(functions)) as pool:
        return list(pool.map(execute, functions))


def report(store):
    return store.report(
        RUN,
        source="openclaw",
        source_event_id="worker:pg:completed:1",
        subject="worker:pg",
        sequence=1,
        kind="worker_brief_completed",
        summary="Commit ready for owner review",
    )


def test_two_connections_and_two_owners_race_acquisition(pg_bridge):
    store, _ = pg_bridge
    barrier = Barrier(2)

    def backend(_):
        with control_plane_connection(store.path) as con:
            pid = con.execute("SELECT pg_backend_pid()").fetchone()[0]
            barrier.wait(timeout=5)  # Hold both independent PG connections.
            return pid

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert len(set(pool.map(backend, range(2)))) == 2
    outcomes = race(
        [
            lambda: store.lease(RUN, owner_id="owner-a"),
            lambda: store.lease(RUN, owner_id="owner-b"),
        ]
    )
    winners = [result for result in outcomes if isinstance(result, dict)]
    assert len(winners) == 1
    assert sum(isinstance(result, ContinuityConflict) for result in outcomes) == 1
    status = store.status(RUN)
    assert status["owner"]["id"] == winners[0]["owner_id"]
    assert status["owner"]["epoch"] == winners[0]["owner_epoch"] == 1


def test_owner_and_intruder_race_renewal_with_epoch_cas(pg_bridge):
    store, clock = pg_bridge
    lease = store.lease(RUN, owner_id="owner-a", lease_seconds=10)
    epoch = lease["owner_epoch"]
    clock.advance(1)
    outcomes = race(
        [
            lambda: store.lease(
                RUN, owner_id="owner-a", owner_epoch=epoch, lease_seconds=30
            ),
            lambda: store.lease(
                RUN, owner_id="owner-b", owner_epoch=epoch, lease_seconds=60
            ),
        ]
    )
    assert isinstance(outcomes[0], dict)
    assert isinstance(outcomes[1], ContinuityConflict)
    status = store.status(RUN)
    assert status["owner"]["id"] == "owner-a"
    assert status["owner"]["epoch"] == epoch
    until = status["owner"]["lease_until"]
    with pytest.raises(ContinuityConflict, match="epoch changed"):
        store.lease(RUN, owner_id="owner-a", owner_epoch=epoch + 1)
    assert store.status(RUN)["owner"]["lease_until"] == until


def test_expired_readmission_race_and_stale_owner_ack(pg_bridge):
    store, clock = pg_bridge
    old = store.lease(RUN, owner_id="old-owner", lease_seconds=5)
    event_id = report(store)
    action = store.consume(RUN, owner_id="old-owner", owner_epoch=old["owner_epoch"])
    clock.advance(5)
    with pytest.raises(ContinuityConflict):
        store.acknowledge(
            RUN,
            owner_id="old-owner",
            owner_epoch=old["owner_epoch"],
            event_id=event_id,
            checkpoint="Stale continuation",
        )
    outcomes = race(
        [
            lambda: store.lease(
                RUN, owner_id="new-a", owner_epoch=old["owner_epoch"], lease_seconds=5
            ),
            lambda: store.lease(
                RUN, owner_id="new-b", owner_epoch=old["owner_epoch"], lease_seconds=5
            ),
        ]
    )
    [winner] = [result for result in outcomes if isinstance(result, dict)]
    assert winner["owner_epoch"] == old["owner_epoch"] + 1
    fence = {"owner_id": winner["owner_id"], "owner_epoch": winner["owner_epoch"]}
    assert store.consume(RUN, **fence) == action
    with pytest.raises(ContinuityConflict):
        store.acknowledge(
            RUN,
            owner_id="old-owner",
            owner_epoch=old["owner_epoch"],
            event_id=event_id,
            checkpoint="Stale continuation",
        )
    clock.advance(5)
    # The newer lease is also expired: an old delayed renewal still fails CAS.
    with pytest.raises(ContinuityConflict, match="epoch changed"):
        store.lease(RUN, owner_id="old-owner", owner_epoch=old["owner_epoch"])
    assert store.status(RUN)["owner"]["epoch"] == winner["owner_epoch"]
    recovered = store.lease(
        RUN, owner_id=winner["owner_id"], owner_epoch=winner["owner_epoch"]
    )
    assert recovered["owner_epoch"] == winner["owner_epoch"] + 1
    store.acknowledge(
        RUN,
        owner_id=recovered["owner_id"],
        owner_epoch=recovered["owner_epoch"],
        event_id=event_id,
        checkpoint="Current owner reconciled",
    )
    assert store.status(RUN)["checkpoint"] == "Current owner reconciled"


def test_duplicate_source_retries_race_before_and_after_ack(pg_bridge):
    store, _ = pg_bridge
    ids = race([lambda: report(store), lambda: report(store)])
    assert ids[0] == ids[1] and isinstance(ids[0], str)
    assert store.status(RUN)["inbox_counts"] == {"pending": 1}
    with pytest.raises(ContinuityConflict, match="divergent"):
        store.report(
            RUN,
            source="openclaw",
            source_event_id="worker:pg:completed:1",
            subject="worker:pg",
            sequence=1,
            kind="worker_awaiting_input",
            summary="Changed identity",
        )
    lease = store.lease(RUN, owner_id="owner")
    fence = {"owner_id": "owner", "owner_epoch": lease["owner_epoch"]}
    action = store.consume(RUN, **fence)
    assert action["type"] == "review_worker_result"
    store.acknowledge(RUN, **fence, event_id=ids[0], checkpoint="Owner reviewed commit")
    assert race([lambda: report(store), lambda: report(store)]) == ids
    assert store.consume(RUN, **fence) is None


def test_retry_pool_restart_persistence_and_bounded_exhaustion(pg_bridge):
    store, clock = pg_bridge
    lease = store.lease(RUN, owner_id="owner", lease_seconds=5)
    fence = {"owner_id": "owner", "owner_epoch": lease["owner_epoch"]}
    event_id = report(store)
    action = store.consume(RUN, **fence)
    config = control_store_config(store.path)
    close_control_store(store.path)  # Close/reconstruct clients, never restart PG.
    configure_control_store(store.path, config)
    restarted = FactoryObserverContinuity(store.path, clock=clock)
    assert restarted.status(RUN)["next_action"] == action
    assert restarted.status(RUN)["events"][0]["attempts"] == 1
    assert restarted.consume(RUN, **fence) is None
    clock.advance(5)
    assert restarted.status(RUN)["recovery"] == "reclaim_expired_owner"
    lease = restarted.lease(RUN, owner_id="recovered")
    fence = {"owner_id": "recovered", "owner_epoch": lease["owner_epoch"]}
    assert restarted.consume(RUN, **fence) == action
    clock.advance(10)
    assert restarted.consume(RUN, **fence) == action
    clock.advance(20)
    assert restarted.consume(RUN, **fence) is None
    assert restarted.status(RUN)["recovery"] == "manual_attention"
    assert restarted.status(RUN)["events"][0]["attempts"] == 3
    restarted.acknowledge(
        RUN, **fence, event_id=event_id, checkpoint="Recovered checkpoint"
    )
    assert report(restarted) == event_id
    close_control_store(store.path)
    configure_control_store(store.path, config)
    recovered = FactoryObserverContinuity(store.path, clock=clock)
    assert recovered.status(RUN)["checkpoint"] == "Recovered checkpoint"
    assert recovered.status(RUN)["inbox_counts"] == {"acknowledged": 1}
    assert recovered.consume(RUN, **fence) is None
