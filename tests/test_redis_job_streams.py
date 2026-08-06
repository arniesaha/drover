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
        self.now_ms = 1_000_000
        self.xclaim_calls: list[dict] = []

    def advance(self, milliseconds: int) -> None:
        self.now_ms += milliseconds

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
                "last_delivered_ms": self.now_ms,
            }
            out.append((entry_id, dict(fields)))
        return [(name, out)] if out else []

    def xpending_range(self, name, groupname, min, max, count):
        pel = self.groups[(name, groupname)]["pel"]
        if min not in {"-", "0-0"} and max == min:
            ids = [min] if min in pel else []
        else:
            ids = sorted(pel, key=_id_key)
            if isinstance(min, str) and min.startswith("("):
                cursor = _id_key(min[1:])
                ids = [entry_id for entry_id in ids if _id_key(entry_id) > cursor]
            ids = ids[:count]
        return [
            {
                "message_id": entry_id,
                "consumer": pel[entry_id]["consumer"],
                "time_since_delivered": (
                    self.now_ms - pel[entry_id]["last_delivered_ms"]
                    if self.now_ms >= pel[entry_id]["last_delivered_ms"]
                    else 0
                ),
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
            if self.now_ms - pe["last_delivered_ms"] < min_idle_time:
                continue
            pe["consumer"] = consumername
            pe["times_delivered"] += 1
            pe["last_delivered_ms"] = self.now_ms
            out.append((entry_id, dict(entries_by_id[entry_id])))
        return ("0-0", out, [])

    def xclaim(
        self,
        name,
        groupname,
        consumername,
        min_idle_time,
        message_ids,
        **kwargs,
    ):
        self.xclaim_calls.append(
            {
                "name": name,
                "group": groupname,
                "consumer": consumername,
                "min_idle_time": min_idle_time,
                "message_ids": list(message_ids),
                **kwargs,
            }
        )
        pel = self.groups[(name, groupname)]["pel"]
        entries = dict(self.streams.get(name, []))
        out = []
        for entry_id in message_ids:
            if entry_id not in pel:
                continue
            pe = pel[entry_id]
            pe["consumer"] = consumername
            if kwargs.get("time") is not None:
                # Redis clamps future XCLAIM TIME values to server time.
                pe["last_delivered_ms"] = min(int(kwargs["time"]), self.now_ms)
            if kwargs.get("retrycount") is not None:
                pe["times_delivered"] = int(kwargs["retrycount"])
            else:
                pe["times_delivered"] += 1
            pe["last_delivered_ms"] = self.now_ms
            out.append((entry_id, dict(entries[entry_id])))
        return out

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

    def time(self):
        return self.now_ms // 1000, (self.now_ms % 1000) * 1000

    def zadd(self, name, mapping):
        self.hashes.setdefault(name, {}).update(mapping)
        return len(mapping)

    def zscore(self, name, key):
        return self.hashes.get(name, {}).get(key)

    def zrem(self, name, key):
        return 1 if self.hashes.get(name, {}).pop(key, None) is not None else 0


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


def test_redis_adapter_defers_without_spending_transport_attempts():
    client = FakeRedis()
    stream = RedisJobStream(
        client,
        RedisJobStreamConfig(
            stream="summarize_jobs",
            visibility_timeout_ms=60_000,
            max_deliveries=5,
        ),
    )
    stream.add({"session_id": "s1"})
    (delivery,) = stream.read_group("worker-a")
    due_ms = client.now_ms + 240_000

    assert stream.defer(delivery.id, until_ms=due_ms) is True
    for _ in range(3):
        client.advance(60_000)
        assert stream.reclaim("worker-b") == []
        assert stream.pending()[0].delivery_count == 1

    client.advance(60_000)
    (reclaimed,) = stream.reclaim("worker-b")
    assert reclaimed.delivery_count == 2
    assert client.xclaim_calls[0].get("time") is None


def test_redis_adapter_backoffs_preserve_five_backend_executions():
    client = FakeRedis()
    stream = RedisJobStream(
        client,
        RedisJobStreamConfig(
            stream="summarize_jobs",
            visibility_timeout_ms=60_000,
            max_deliveries=5,
        ),
    )
    stream.add({"session_id": "s1", "source_version": "v1"})
    (delivery,) = stream.read_group("worker-a")
    backend_executions = 1

    while backend_executions < 5:
        base_seconds = min(60 * (2 ** (backend_executions - 1)), 3600)
        due_ms = client.now_ms + base_seconds * 1000
        stream.fail(delivery.id, "backend failed")
        assert stream.defer(delivery.id, until_ms=due_ms) is True
        while client.now_ms < due_ms:
            client.advance(min(60_000, due_ms - client.now_ms))
            reclaimed = stream.reclaim("worker-b")
            if client.now_ms < due_ms:
                assert reclaimed == []
                continue
            (delivery,) = reclaimed
        backend_executions += 1

    assert backend_executions == 5
    assert delivery.delivery_count == 5
    assert stream.dead_letters() == []
    assert stream.ack(delivery.id) is True


def test_redis_reclaim_pages_past_deferred_prefix_without_claiming_it():
    client = FakeRedis()
    stream = RedisJobStream(
        client,
        RedisJobStreamConfig(
            stream="summarize_jobs",
            visibility_timeout_ms=0,
            max_deliveries=5,
        ),
    )
    deferred_ids = []
    for index in range(1_001):
        stream.add({"session_id": f"deferred-{index}"})
        (delivery,) = stream.read_group("worker-a")
        deferred_ids.append(delivery.id)
        assert stream.defer(delivery.id, until_ms=client.now_ms + 60_000)
    stream.add({"session_id": "due"})
    (due_delivery,) = stream.read_group("worker-a")

    (reclaimed,) = stream.reclaim("worker-b", count=1)

    assert reclaimed.id == due_delivery.id
    assert reclaimed.delivery_count == 2
    assert client.xclaim_calls == [
        {
            "name": "summarize_jobs",
            "group": "workers",
            "consumer": "worker-b",
            "min_idle_time": 0,
            "message_ids": [due_delivery.id],
        }
    ]
    pending_by_id = {entry.id: entry for entry in stream.pending()}
    assert pending_by_id[deferred_ids[0]].delivery_count == 1


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
