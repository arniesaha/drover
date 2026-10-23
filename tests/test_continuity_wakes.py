from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest

from drover.schema import bootstrap_control_plane_store
from drover.server.continuity_wakes import ContinuityWakeConflict, ContinuityWakes

ISSUE = "arniesaha/drover#784"
OWNER = "openclaw-owner"


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 23, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def wakes(tmp_path):
    path = tmp_path / "control.duckdb"
    bootstrap_control_plane_store(path)
    clock = Clock()
    return ContinuityWakes(path, clock=clock, visibility_seconds=5), clock


def test_wake_identity_is_durable_and_divergence_is_refused(wakes):
    store, _ = wakes
    first = store.enqueue(
        issue_key=ISSUE,
        action="wake",
        fingerprint="worker-idle:session-7",
        payload={"reason": "idle", "session_id": "session-7"},
    )
    assert (
        store.enqueue(
            issue_key=ISSUE,
            action="wake",
            fingerprint="worker-idle:session-7",
            payload={"reason": "idle", "session_id": "session-7"},
        )
        == first
    )
    with pytest.raises(ContinuityWakeConflict, match="divergent"):
        store.enqueue(
            issue_key=ISSUE,
            action="wake",
            fingerprint="worker-idle:session-7",
            payload={"reason": "different"},
        )
    assert store.status(ISSUE)["wake_counts"] == {"pending": 1}


def test_killed_owner_turn_is_redelivered_then_acknowledged(wakes):
    store, clock = wakes
    wake_id = store.enqueue(
        issue_key=ISSUE,
        action="wake",
        fingerprint="idle:one",
        payload={"reason": "idle"},
    )
    lease = store.lease(ISSUE, owner_id=OWNER)
    first = store.consume(ISSUE, owner_id=OWNER, owner_epoch=lease["owner_epoch"])
    assert first["wake_id"] == wake_id
    assert first["attempts"] == 1
    assert (
        store.consume(ISSUE, owner_id=OWNER, owner_epoch=lease["owner_epoch"]) is None
    )

    # Simulate a killed orchestrator after it received the wake but before ack.
    clock.advance(5)
    redelivery = store.consume(ISSUE, owner_id=OWNER, owner_epoch=lease["owner_epoch"])
    assert redelivery["wake_id"] == wake_id
    assert redelivery["attempts"] == 2
    store.acknowledge(
        ISSUE,
        owner_id=OWNER,
        owner_epoch=lease["owner_epoch"],
        wake_id=wake_id,
        delivery_token=redelivery["delivery_token"],
    )
    assert store.status(ISSUE)["wake_counts"] == {"acknowledged": 1}


def test_attempt_budget_exhausts_unacknowledged_wake(wakes):
    store, clock = wakes
    store.enqueue(issue_key=ISSUE, action="wake", fingerprint="idle:two", payload={})
    lease = store.lease(ISSUE, owner_id=OWNER, lease_seconds=300)
    epoch = lease["owner_epoch"]
    for attempts in range(1, 4):
        delivery = store.consume(ISSUE, owner_id=OWNER, owner_epoch=epoch)
        assert delivery["attempts"] == attempts
        clock.advance(5 * 2 ** (attempts - 1))
    assert store.consume(ISSUE, owner_id=OWNER, owner_epoch=epoch) is None
    assert store.status(ISSUE)["wake_counts"] == {"exhausted": 1}


def test_stale_epoch_cannot_consume_or_ack_after_reclaim(wakes):
    store, clock = wakes
    wake_id = store.enqueue(
        issue_key=ISSUE, action="wake", fingerprint="idle:three", payload={}
    )
    first = store.lease(ISSUE, owner_id=OWNER, lease_seconds=5)
    store.consume(ISSUE, owner_id=OWNER, owner_epoch=first["owner_epoch"])
    clock.advance(6)
    replacement = store.lease(ISSUE, owner_id="replacement")
    with pytest.raises(ContinuityWakeConflict, match="current owner lease"):
        store.acknowledge(
            ISSUE,
            owner_id=OWNER,
            owner_epoch=first["owner_epoch"],
            wake_id=wake_id,
            delivery_token="a" * 32,
        )
    with pytest.raises(ContinuityWakeConflict, match="current owner lease"):
        store.consume(ISSUE, owner_id=OWNER, owner_epoch=first["owner_epoch"])
    assert replacement["owner_epoch"] == first["owner_epoch"] + 1


def test_concurrent_enqueue_reselects_one_durable_identity(wakes):
    store, _ = wakes
    barrier = Barrier(2)

    def enqueue():
        barrier.wait()
        return store.enqueue(
            issue_key=ISSUE,
            action="wake",
            fingerprint="concurrent",
            payload={"reason": "idle"},
        )

    with ThreadPoolExecutor(max_workers=2) as workers:
        identities = list(workers.map(lambda _: enqueue(), range(2)))
    assert identities[0] == identities[1]
    assert store.status(ISSUE)["wake_counts"] == {"pending": 1}


def test_stale_delivery_cannot_acknowledge_redelivery(wakes):
    store, clock = wakes
    wake_id = store.enqueue(
        issue_key=ISSUE, action="wake", fingerprint="stale-ack", payload={}
    )
    epoch = store.lease(ISSUE, owner_id=OWNER)["owner_epoch"]
    first = store.consume(ISSUE, owner_id=OWNER, owner_epoch=epoch)
    clock.advance(5)
    second = store.consume(ISSUE, owner_id=OWNER, owner_epoch=epoch)
    assert second["wake_id"] == wake_id
    assert second["delivery_token"] != first["delivery_token"]
    with pytest.raises(
        ContinuityWakeConflict, match="different owner epoch or delivery"
    ):
        store.acknowledge(
            ISSUE,
            owner_id=OWNER,
            owner_epoch=epoch,
            wake_id=wake_id,
            delivery_token=first["delivery_token"],
        )
    store.acknowledge(
        ISSUE,
        owner_id=OWNER,
        owner_epoch=epoch,
        wake_id=wake_id,
        delivery_token=second["delivery_token"],
    )
    # Retrying the exact acknowledged delivery is idempotent.
    store.acknowledge(
        ISSUE,
        owner_id=OWNER,
        owner_epoch=epoch,
        wake_id=wake_id,
        delivery_token=second["delivery_token"],
    )


def test_postgres_concurrent_enqueue_and_stale_delivery_ack(pg_control_path):
    """Use the real PostgreSQL adapter for the conflict/re-delivery contract."""
    clock = Clock()
    store = ContinuityWakes(pg_control_path, clock=clock, visibility_seconds=5)
    barrier = Barrier(2)

    def enqueue():
        barrier.wait()
        return store.enqueue(
            issue_key=ISSUE,
            action="wake",
            fingerprint="postgres-concurrent",
            payload={"reason": "idle"},
        )

    with ThreadPoolExecutor(max_workers=2) as workers:
        identities = list(workers.map(lambda _: enqueue(), range(2)))
    assert identities[0] == identities[1]

    epoch = store.lease(ISSUE, owner_id=OWNER)["owner_epoch"]
    first = store.consume(ISSUE, owner_id=OWNER, owner_epoch=epoch)
    clock.advance(5)
    second = store.consume(ISSUE, owner_id=OWNER, owner_epoch=epoch)
    with pytest.raises(
        ContinuityWakeConflict, match="different owner epoch or delivery"
    ):
        store.acknowledge(
            ISSUE,
            owner_id=OWNER,
            owner_epoch=epoch,
            wake_id=first["wake_id"],
            delivery_token=first["delivery_token"],
        )
    store.acknowledge(
        ISSUE,
        owner_id=OWNER,
        owner_epoch=epoch,
        wake_id=second["wake_id"],
        delivery_token=second["delivery_token"],
    )
