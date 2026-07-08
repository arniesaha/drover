"""Tests for the Redis Streams adapter behind derived-job reliability."""

from __future__ import annotations

from drover.server.jobs import RedisJobStream, RedisJobStreamConfig


class FakeRedis:
    """Tiny command-surface fake for Redis Streams consumer-group tests."""

    def __init__(self) -> None:
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.groups: dict[tuple[str, str], dict] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.seq = 0

    def ping(self) -> bool:
        return True

    def xgroup_create(self, name: str, groupname: str, id: str, mkstream: bool) -> None:
        if mkstream:
            self.streams.setdefault(name, [])
        key = (name, groupname)
        if key in self.groups:
            raise RuntimeError("BUSYGROUP Consumer Group name already exists")
        self.groups[key] = {"cursor_id": "0-0", "pel": {}}

    def xadd(self, name: str, fields: dict[str, str], **_kw) -> str:
        self.seq += 1
        entry_id = f"{self.seq}-0"
        self.streams.setdefault(name, []).append((entry_id, dict(fields)))
        return entry_id

    def xreadgroup(self, groupname, consumername, streams, count=1, block=0):
        name = next(iter(streams))
        group = self.groups[(name, groupname)]
        entries = self.streams.get(name, [])
        out = []
        for entry_id, fields in entries:
            if _id_key(entry_id) <= _id_key(group["cursor_id"]):
                continue
            if len(out) >= count:
                break
            group["cursor_id"] = entry_id
            group["pel"][entry_id] = {
                "consumer": consumername,
                "times_delivered": 1,
                "time_since_delivered": 0,
            }
            out.append((entry_id, dict(fields)))
        return [(name, out)] if out else []

    def xpending_range(self, name, groupname, min, max, count):
        pel = self.groups[(name, groupname)]["pel"]
        if min not in {"-", "0-0"} and max == min:
            ids = [min] if min in pel else []
        else:
            ids = sorted(pel, key=lambda s: tuple(map(int, s.split("-"))))[:count]
        return [
            {
                "message_id": entry_id,
                "consumer": pel[entry_id]["consumer"],
                "time_since_delivered": pel[entry_id]["time_since_delivered"],
                "times_delivered": pel[entry_id]["times_delivered"],
            }
            for entry_id in ids
        ]

    def xautoclaim(
        self, name, groupname, consumername, min_idle_time, start_id, count=10
    ):
        group = self.groups[(name, groupname)]
        entries_by_id = dict(self.streams.get(name, []))
        out = []
        for entry_id in sorted(
            group["pel"], key=lambda s: tuple(map(int, s.split("-")))
        ):
            if len(out) >= count:
                break
            pe = group["pel"][entry_id]
            pe["consumer"] = consumername
            pe["times_delivered"] += 1
            out.append((entry_id, dict(entries_by_id[entry_id])))
        return ("0-0", out, [])

    def xack(self, name, groupname, entry_id):
        return 1 if self.groups[(name, groupname)]["pel"].pop(entry_id, None) else 0

    def xdel(self, name, entry_id):
        entries = self.streams.get(name, [])
        before = len(entries)
        self.streams[name] = [item for item in entries if item[0] != entry_id]
        return before - len(self.streams[name])

    def xlen(self, name):
        return len(self.streams.get(name, []))

    def xrange(self, name, min="-", max="+", count=None):
        entries = list(self.streams.get(name, []))
        if min not in {"-", "0-0"} and max == min:
            entries = [item for item in entries if item[0] == min]
        return entries[:count] if count is not None else entries

    def hset(self, name, key, value):
        self.hashes.setdefault(name, {})[key] = value

    def hget(self, name, key):
        return self.hashes.get(name, {}).get(key)

    def hdel(self, name, key):
        self.hashes.get(name, {}).pop(key, None)


def _id_key(entry_id: str) -> tuple[int, int]:
    left, _, right = entry_id.partition("-")
    return int(left), int(right or 0)


def make_stream(**kw) -> RedisJobStream:
    return RedisJobStream(
        FakeRedis(),
        RedisJobStreamConfig(
            stream="summarize_jobs",
            visibility_timeout_ms=0,
            high_water=2,
            **kw,
        ),
    )


def test_redis_adapter_delivers_each_job_to_one_consumer_then_acks():
    stream = make_stream()
    stream.add({"session_id": "s1"})
    stream.add({"session_id": "s2"})

    a = stream.read_group("worker-a", count=1)
    b = stream.read_group("worker-b", count=1)

    assert {a[0].fields["session_id"], b[0].fields["session_id"]} == {"s1", "s2"}
    assert stream.read_group("worker-c") == []
    assert stream.ack(a[0].id) is True
    assert stream.ack(a[0].id) is False


def test_redis_adapter_recovers_crash_before_ack_with_xautoclaim():
    stream = make_stream(max_deliveries=3)
    stream.add({"session_id": "s1"})
    (job,) = stream.read_group("worker-a")
    stream.fail(job.id, "runtime timeout")

    (reclaimed,) = stream.reclaim("worker-b")

    assert reclaimed.id == job.id
    assert reclaimed.fields["session_id"] == "s1"
    assert reclaimed.delivery_count == 2
    assert stream.pending()[0].consumer == "worker-b"


def test_redis_adapter_crash_after_durable_write_before_ack_is_redelivered_once():
    stream = make_stream(max_deliveries=3)
    stream.add({"session_id": "s1"})
    (job,) = stream.read_group("worker-a")

    durable_writes = {"s1"}  # worker crashed after this write, before ACK.
    (reclaimed,) = stream.reclaim("worker-b")
    assert reclaimed.fields["session_id"] in durable_writes

    # The worker sees the durable projection already exists and ACKs without
    # rewriting it. That is the worker-side idempotency contract.
    assert stream.ack(reclaimed.id) is True
    assert stream.pending() == []
    assert stream.length() == 0


def test_redis_adapter_routes_exhausted_retries_to_redacted_dlq_and_replays():
    stream = make_stream(max_deliveries=1)
    stream.add({"session_id": "s1", "kind": "incremental"})
    (job,) = stream.read_group("worker-a")
    stream.fail(job.id, "Bearer sk-secret-token for user@example.com failed")

    assert stream.reclaim("worker-b") == []
    (dead,) = stream.dead_letters()

    assert dead.fields == {"session_id": "s1", "kind": "incremental"}
    assert dead.source_id == job.id
    assert "[REDACTED_SECRET]" in (dead.last_error or "")
    assert "[REDACTED_EMAIL]" in (dead.last_error or "")

    new_id = stream.replay(dead.dead_id)
    assert new_id is not None
    assert stream.dead_letters() == []
    (redelivered,) = stream.read_group("worker-c")
    assert redelivered.fields["session_id"] == "s1"


def test_redis_adapter_backpressure_counts_pending_and_undelivered():
    stream = make_stream()
    stream.add({"session_id": "s1"})
    stream.add({"session_id": "s2"})
    assert stream.backpressure()["should_shed"] is True

    (job,) = stream.read_group("worker-a")
    assert stream.backpressure()["pending"] == 1
    assert stream.backpressure()["undelivered"] == 1
    stream.ack(job.id)
    assert stream.backpressure()["should_shed"] is False
