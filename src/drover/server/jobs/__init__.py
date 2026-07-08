"""Job-queue primitives for derived Drover workers.

Today the ``*_jobs`` tables in DuckDB act as single-process work queues
(claim-by-conditional-update, an ``attempts`` counter, and an ``errored``
terminal state). They have no redelivery, no dead-letter path, and no
backpressure signal — a job that fails sticks in ``errored`` until an
operator runs ``retry_errored_jobs``.

This package holds the design target: a Redis Streams *consumer-group*
model with at-least-once delivery, automatic redelivery of stalled work,
a dead-letter stream that preserves replay context, and a length-based
backpressure signal.

``streams.JobStream`` is a pure-Python reference implementation of that
model. It mirrors the Redis Streams command semantics one-for-one (each
method names the command it stands in for) so it doubles as executable
documentation and as the contract a thin ``redis-py`` adapter must honour.
It needs no running Redis, so QA can exercise the happy path and the
failure path deterministically. See
``docs/design/redis-job-queue-retry-dlq.md``.
"""

from drover.server.jobs.redis_streams import RedisJobStream, RedisJobStreamConfig
from drover.server.jobs.streams import DeadLetter, Delivery, JobStream, PendingEntry

__all__ = [
    "JobStream",
    "RedisJobStream",
    "RedisJobStreamConfig",
    "Delivery",
    "PendingEntry",
    "DeadLetter",
]
