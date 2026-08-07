"""Behavioural spec for the consumer-group / retry / DLQ reference model.

These tests pin the semantics QA verifies for AGE-33: a happy path where a
job is delivered, processed, and acked exactly once, and a failure path
where a stalled job is redelivered until it exhausts its retry budget and
lands in the dead-letter stream with enough context to replay.

A manual clock drives idle/visibility timing so nothing depends on sleeps.
"""

from __future__ import annotations

import pytest

from drover.server.jobs import JobStream


class FakeClock:
    """Monotonic millisecond clock the tests advance explicitly."""

    def __init__(self, start: int = 1_000) -> None:
        self.now = start

    def __call__(self) -> int:
        return self.now

    def advance(self, ms: int) -> None:
        self.now += ms


def make_stream(**kw) -> tuple[JobStream, FakeClock]:
    clock = FakeClock()
    stream = JobStream("summarize_jobs", clock=clock, **kw)
    return stream, clock


# --- happy path ---------------------------------------------------------


def test_happy_path_deliver_once_then_ack():
    stream, _ = make_stream()
    stream.add({"session_id": "s1"})

    delivered = stream.read_group("worker-a", count=10)
    assert [d.fields["session_id"] for d in delivered] == ["s1"]
    assert delivered[0].delivery_count == 1

    # While un-acked it is owned by worker-a and not redelivered to anyone.
    assert stream.read_group("worker-b") == []
    assert [p.consumer for p in stream.pending()] == ["worker-a"]

    assert stream.ack(delivered[0].id) is True
    assert stream.pending() == []
    assert stream.length() == 0


def test_ack_is_idempotent_and_flags_superseded_work():
    stream, _ = make_stream()
    eid = stream.add({"session_id": "s1"})
    (d,) = stream.read_group("worker-a")
    assert d.id == eid
    assert stream.ack(eid) is True
    # Second ack returns False so a racing worker knows its result is stale.
    assert stream.ack(eid) is False


def test_each_entry_goes_to_exactly_one_consumer():
    stream, _ = make_stream()
    for i in range(4):
        stream.add({"session_id": f"s{i}"})

    a = stream.read_group("worker-a", count=2)
    b = stream.read_group("worker-b", count=2)
    ids = {d.id for d in a} | {d.id for d in b}
    assert len(ids) == 4  # no overlap
    assert stream.undelivered() == 0


# --- failure path -------------------------------------------------------


def test_stalled_job_is_reclaimed_and_redelivered():
    stream, clock = make_stream(visibility_timeout_ms=60_000, max_deliveries=5)
    stream.add({"session_id": "s1"})
    (d,) = stream.read_group("worker-a")
    stream.fail(d.id, "ollama timeout")  # worker-a never acks

    # Too soon: still owned by worker-a.
    clock.advance(30_000)
    assert stream.reclaim("worker-b") == []

    # Past the visibility timeout: worker-b reclaims it, retry counter bumps.
    clock.advance(31_000)
    reclaimed = stream.reclaim("worker-b")
    assert len(reclaimed) == 1
    assert reclaimed[0].delivery_count == 2
    assert [p.consumer for p in stream.pending()] == ["worker-b"]


def test_deferred_job_does_not_spend_delivery_before_due():
    stream, clock = make_stream(visibility_timeout_ms=60_000, max_deliveries=5)
    stream.add({"session_id": "s1"})
    (delivery,) = stream.read_group("worker-a")

    assert stream.defer(delivery.id, until_ms=clock.now + 240_000) is True
    for _ in range(3):
        clock.advance(60_000)
        assert stream.reclaim("worker-b") == []
        assert stream.pending()[0].delivery_count == 1

    clock.advance(60_000)
    (reclaimed,) = stream.reclaim("worker-b")
    assert reclaimed.delivery_count == 2


def test_exhausted_retries_go_to_dead_letter_with_replay_context():
    stream, clock = make_stream(visibility_timeout_ms=1_000, max_deliveries=3)
    stream.add({"session_id": "s1", "kind": "incremental"})

    (d,) = stream.read_group("worker-a")  # delivery 1
    stream.fail(d.id, "503 backend unavailable")

    # Reclaim twice (deliveries 2 and 3), failing each time.
    for _ in range(2):
        clock.advance(2_000)
        reclaimed = stream.reclaim("worker-a")
        assert len(reclaimed) == 1
        stream.fail(reclaimed[0].id, "503 backend unavailable")

    # delivery_count is now 3 == max; the next reclaim dead-letters it
    # instead of redelivering.
    clock.advance(2_000)
    assert stream.reclaim("worker-a") == []
    assert stream.pending() == []
    assert stream.length() == 0

    dead = stream.dead_letters()
    assert len(dead) == 1
    dl = dead[0]
    # Replay/debug context is preserved.
    assert dl.fields == {"session_id": "s1", "kind": "incremental"}
    assert dl.delivery_count == 3
    assert dl.last_error == "503 backend unavailable"
    assert dl.source_id == d.id
    assert dl.enqueued_ms <= dl.dead_lettered_ms


def test_replay_reinjects_dead_letter_for_another_attempt():
    stream, clock = make_stream(visibility_timeout_ms=1_000, max_deliveries=1)
    stream.add({"session_id": "s1"})
    (d,) = stream.read_group("worker-a")
    stream.fail(d.id, "transient")
    clock.advance(2_000)
    stream.reclaim("worker-a")  # max_deliveries=1 -> straight to DLQ

    (dl,) = stream.dead_letters()
    new_id = stream.replay(dl.dead_id)
    assert new_id is not None
    assert stream.dead_letters() == []  # consumed from the dead stream

    redelivered = stream.read_group("worker-b")
    assert [x.fields["session_id"] for x in redelivered] == ["s1"]
    assert stream.replay("nope-0") is None  # unknown id is a no-op


# --- backpressure -------------------------------------------------------


def test_backpressure_trips_at_high_water():
    stream, _ = make_stream(high_water=3)
    for i in range(2):
        stream.add({"session_id": f"s{i}"})
    bp = stream.backpressure()
    assert bp["should_shed"] is False
    assert bp["backlog"] == 2

    stream.add({"session_id": "s2"})
    bp = stream.backpressure()
    assert bp["backlog"] == 3
    assert bp["should_shed"] is True
    assert bp["pending"] == 0 and bp["undelivered"] == 3


def test_pending_counts_toward_backpressure_until_acked():
    stream, _ = make_stream(high_water=2)
    stream.add({"a": 1})
    stream.add({"a": 2})
    delivered = stream.read_group("worker-a", count=2)
    # Delivered-but-unacked work still counts as backlog.
    assert stream.backpressure()["backlog"] == 2
    assert stream.backpressure()["pending"] == 2
    for d in delivered:
        stream.ack(d.id)
    assert stream.backpressure()["backlog"] == 0


def test_maxlen_trims_only_acked_entries():
    stream, _ = make_stream(maxlen=2)
    e0 = stream.add({"a": 0})
    stream.add({"a": 1})
    stream.add({"a": 2})  # exceeds maxlen, but e0 is still live -> kept
    assert stream.length() == 3

    (d,) = stream.read_group("worker-a")
    assert d.id == e0
    stream.ack(e0)
    # Now the oldest live id advanced; a further add can trim the acked head.
    stream.add({"a": 3})
    assert stream.length() <= 3


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
