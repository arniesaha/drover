# Redis consumer-group retry & DLQ for derived Nexus jobs

Status: **reference prototype + Redis adapter** (AGE-33 / #151). The
dependency-free reference model lives in
[`src/nexus/server/jobs/streams.py`](../../src/nexus/server/jobs/streams.py).
The production-shaped Redis adapter lives in
[`src/nexus/server/jobs/redis_streams.py`](../../src/nexus/server/jobs/redis_streams.py).
Both are exercised in CI by [`tests/test_job_streams.py`](../../tests/test_job_streams.py)
and [`tests/test_redis_job_streams.py`](../../tests/test_redis_job_streams.py).

## Why

Derived data in Nexus is produced by background workers draining queues:
`summarize_jobs → session_summaries`, `brief_jobs → project_briefs`,
`embed_jobs → session_embeddings`, `span_embed_jobs → span_embeddings`
(see [`architecture.md`](../architecture.md)). Today those queues are rows
in DuckDB with this shape:

```sql
CREATE TABLE summarize_jobs (
  session_id  VARCHAR PRIMARY KEY,
  status      VARCHAR,    -- 'pending' | 'running' | 'done' | 'errored'
  attempts    INTEGER DEFAULT 0,
  last_error  VARCHAR,
  enqueued_at TIMESTAMP,
  updated_at  TIMESTAMP
);
```

A worker claims a row with a conditional `UPDATE ... WHERE status='pending'`
(optimistic concurrency), processes it, and sets `done` or `errored`
(`src/nexus/server/summarizer/worker.py`). This works for one process but
has gaps the AGE-33 acceptance criteria call out:

| Gap | Today | Consequence |
|---|---|---|
| **Ownership** | "running" is a flag, not an owner. | A worker that crashes mid-job leaves the row stuck in `running` forever — no other worker reclaims it. |
| **Redelivery** | None. | A transient failure (`errored`) needs a manual `nexus-server retry` (`summarizer/retry.py`) to requeue. |
| **Retry budget** | `attempts` increments but nothing caps it. | A poison job can be requeued forever. |
| **Dead-letter** | None. | `errored` rows pile up in the live table, mixed with retryable ones. |
| **Backpressure** | None. | A burst of SessionEnds can enqueue unboundedly; nothing tells producers to slow down. |

Redis Streams consumer groups solve exactly this set of problems with
primitives we'd otherwise reinvent in SQL. This document specifies the
mapping. The reference prototype makes it executable without a Redis server;
the adapter maps the same contract onto Redis commands while DuckDB / Parquet
remain durable truth.

## Model

One Redis **stream** per job kind, one **consumer group** per stream, N
worker processes joining that group as distinct consumers, plus a
**dead-letter stream** alongside each.

```
summarize_jobs            (stream)      XADD by producers
  └─ group: workers                     one delivery per entry, group-wide
       ├─ consumer: host-a/pid-123      owns entries until it XACKs
       └─ consumer: host-b/pid-456
summarize_jobs:dead       (stream)      exhausted entries, with replay context
```

Delivery is **at-least-once**. An entry enters the group's Pending Entries
List (PEL) the instant a consumer reads it and stays there until that
consumer `XACK`s it. Workers must therefore be idempotent on the write
side — the existing `INSERT OR REPLACE INTO session_summaries` already is,
keyed by `session_id`.

### Command mapping

| Operation | Redis command | Prototype method |
|---|---|---|
| Enqueue a job | `XADD <s> [MAXLEN ~ N] * field val …` | `JobStream.add(fields)` |
| Worker claims new work | `XREADGROUP GROUP <g> <c> COUNT n STREAMS <s> >` | `read_group(consumer, count)` |
| Worker finishes | `XACK <s> <g> <id>` (+ `XDEL`) | `ack(id)` |
| Worker fails | *(leave un-acked — no command)* | `fail(id, error)` |
| Reclaim stalled work | `XAUTOCLAIM <s> <g> <c> <min-idle> 0` | `reclaim(consumer, count)` |
| Inspect in-flight | `XPENDING <s> <g>` | `pending()` |
| Queue depth | `XLEN <s>` | `length()` / `undelivered()` |
| Read dead-letters | `XRANGE <s>:dead - +` | `dead_letters()` |
| Replay a dead-letter | `XADD <s> …` from the dead entry | `replay(dead_id)` |

The Redis adapter also uses a small hash, `<stream>:errors`, to attach a
redacted `last_error` to immutable stream entries. Redis stream messages
cannot be edited after `XADD`; the hash lets `fail(id, error)` preserve the
last error for DLQ records while still leaving the live message un-acked in
the Pending Entries List.

### Ownership & ack semantics

* `read_group` only ever returns entries with the special `>` id — entries
  *never delivered to the group before*. Two consumers reading concurrently
  get disjoint sets; an entry is owned by exactly one consumer at a time
  (`test_each_entry_goes_to_exactly_one_consumer`).
* An owned-but-unacked entry is invisible to every other consumer's
  `read_group` until it is reclaimed (`test_happy_path_deliver_once_then_ack`).
* `ack` is idempotent and returns `False` if the entry was already acked or
  reclaimed away — the signal a worker uses to know its result was
  superseded and must not be double-written
  (`test_ack_is_idempotent_and_flags_superseded_work`).

### Retry counters

Every PEL entry carries a **delivery count** (Redis increments it on each
`XREADGROUP`/`XCLAIM` delivery). It is the retry counter:

* First `read_group` → `delivery_count = 1`.
* Each `reclaim` that redelivers → `delivery_count += 1`.
* `fail(id, error)` does **not** touch the server; it just records
  `last_error` so the eventual dead-letter carries it. The entry ages in the
  PEL until a janitor reclaims it after the visibility timeout
  (`test_stalled_job_is_reclaimed_and_redelivered`).

This replaces the unbounded `attempts` column with a bounded budget.

### Dead-letter path

A janitor loop (or any worker, opportunistically) calls `reclaim` with a
`min-idle-time` equal to the **visibility timeout**. For each stalled entry:

* if redelivering would exceed `max_deliveries`, it is **dead-lettered**:
  `XADD`ed to `<stream>:dead` and removed from the live stream;
* otherwise it is reassigned to the reclaiming consumer and redelivered.

The dead-letter record preserves everything needed to replay or debug
(`test_exhausted_retries_go_to_dead_letter_with_replay_context`):

| Field | Purpose |
|---|---|
| `source_id` | original live-stream id, for correlation |
| `fields` | the untouched job payload — replay is a verbatim re-`XADD` |
| `delivery_count` | how many attempts were burned |
| `last_error` | last failure reason recorded via `fail()` |
| `enqueued_ms` / `dead_lettered_ms` | first-seen and gave-up timestamps |

`replay(dead_id)` re-injects the payload into the live stream for another
run and consumes the dead-letter record
(`test_replay_reinjects_dead_letter_for_another_attempt`). The existing
error-classification in `summarizer/retry.py` (auth / rate-limit / runtime /
validation) maps cleanly onto a replay *policy*: auto-replay runtime and
rate-limit dead-letters, hold validation/unknown ones for an operator.

### Backpressure

`backpressure()` reports `backlog = undelivered + pending` against a
`high_water` mark (`XLEN` + `XPENDING` in Redis terms). When `should_shed`
is true the producer side — `enqueue_brief`, `enqueue_embed`, the summarizer
fan-out — should stop enqueueing new derived jobs (or delay) until workers
drain the backlog (`test_backpressure_trips_at_high_water`,
`test_pending_counts_toward_backpressure_until_acked`).

`MAXLEN ~ N` on `XADD` bounds the stream's memory. The prototype's trim is
deliberately conservative: it only drops entries that are already acked, so
backpressure shedding can never silently lose in-flight work
(`test_maxlen_trims_only_acked_entries`).

## Redis adapter

`RedisJobStream` is the thin `redis-py` adapter for the same method contract.
Redis remains optional; install `nexus[redis]` when enabling it:

```python
from nexus.server.jobs import RedisJobStream, RedisJobStreamConfig

stream = RedisJobStream.from_url(
    "redis://127.0.0.1:6379/0",
    RedisJobStreamConfig(stream="summarize_jobs"),
)
```

Worker-side rule: a worker ACKs only after its idempotent durable write
succeeds (`session_summaries`, `project_briefs`, `session_embeddings`,
`span_embeddings`, or the durable ledger row). If a worker crashes before ACK,
`XAUTOCLAIM` redelivers the message. If it crashes after the durable write but
before ACK, the next worker observes the existing durable projection and ACKs
without duplicating the effect.

## QA verification

```bash
python3 -m pytest tests/test_job_streams.py tests/test_redis_job_streams.py -v
```

* **Happy path** — `test_happy_path_deliver_once_then_ack`: a job is
  delivered once, owned exclusively, acked, and leaves the queue empty.
* **Failure path** — `test_exhausted_retries_go_to_dead_letter_with_replay_context`:
  a repeatedly-failing job is redelivered up to its budget, then
  dead-lettered with full replay context; `test_replay_…` shows recovery.
* **Redis adapter path** — `test_redis_adapter_crash_after_durable_write_before_ack_is_redelivered_once`:
  a worker crash after the durable write but before ACK redelivers once, then
  ACKs based on idempotency instead of duplicating the projection.

A manual clock drives all idle/visibility timing, so the suite is
deterministic and needs no Redis server.
