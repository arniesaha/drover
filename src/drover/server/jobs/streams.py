"""In-memory reference implementation of a Redis Streams consumer group.

This is not a Redis client. It is a faithful, dependency-free model of the
exact Redis Streams semantics Drover would rely on for derived-job delivery,
so the retry / dead-letter / backpressure behaviour can be specified and
tested without a server. A production adapter backed by ``redis-py`` must
produce the same observable behaviour; each method documents the command(s)
it stands in for.

Mapping to Redis commands
-------------------------
    add()             -> XADD <stream> [MAXLEN ~ N] * field value ...
    read_group()      -> XREADGROUP GROUP <g> <consumer> COUNT n STREAMS <s> >
    ack()             -> XACK <stream> <g> <id>   (+ XDEL to bound memory)
    fail()            -> (no-op on the server: a failed job is simply left
                          un-acked so it stays in the Pending Entries List)
    reclaim()         -> XAUTOCLAIM <stream> <g> <consumer> <min-idle> 0
                          followed by the dead-letter policy below
    pending()         -> XPENDING <stream> <g>
    dead_letters()    -> XRANGE <stream>:dead - +
    replay()          -> XADD <stream> ... (re-add from the dead stream)
    backpressure()    -> XLEN <stream> + XPENDING summary vs. a high-water mark

Delivery guarantee is *at-least-once*: an entry stays in the group's Pending
Entries List (PEL) from the moment it is read until it is explicitly acked.
A consumer that crashes mid-job leaves its entry in the PEL; another consumer
reclaims it once it has been idle longer than the visibility timeout.
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


def _wall_clock_ms() -> int:
    """Default clock. Tests inject a deterministic one instead."""
    return int(_time.time() * 1000)


@dataclass(frozen=True)
class Delivery:
    """An entry handed to a consumer by ``read_group`` / ``reclaim``.

    ``delivery_count`` is the number of times this entry has been delivered
    to *any* consumer (1 on first read). It is the retry counter: the
    janitor dead-letters an entry once this would exceed ``max_deliveries``.
    """

    id: str
    fields: Dict[str, Any]
    delivery_count: int


@dataclass
class PendingEntry:
    """A row in the group's Pending Entries List (mirrors XPENDING output)."""

    id: str
    consumer: str
    delivery_count: int
    last_delivered_ms: int
    enqueued_ms: int
    last_error: Optional[str] = None


@dataclass(frozen=True)
class DeadLetter:
    """An entry that exhausted its retries.

    Carries everything needed to replay or debug: the original stream id,
    the untouched payload, how many times it was tried, when it first
    entered the queue, and the last error seen.
    """

    dead_id: str
    source_id: str
    fields: Dict[str, Any]
    delivery_count: int
    enqueued_ms: int
    dead_lettered_ms: int
    last_error: Optional[str]


@dataclass
class _Entry:
    id: str
    fields: Dict[str, Any]
    enqueued_ms: int


class JobStream:
    """A single stream + one consumer group + its dead-letter stream.

    Parameters
    ----------
    name:
        Logical queue name (e.g. ``"summarize_jobs"``).
    group:
        Consumer-group name. All workers for this queue share one group, so
        each entry is delivered to exactly one worker at a time.
    max_deliveries:
        Retry budget. An entry delivered more than this many times is routed
        to the dead-letter stream instead of being redelivered.
    visibility_timeout_ms:
        How long an entry may sit un-acked in a consumer's PEL before another
        consumer is allowed to reclaim it (the XAUTOCLAIM ``min-idle-time``).
    maxlen:
        Approximate cap on the live stream length for backpressure (XADD
        ``MAXLEN ~``). ``None`` disables trimming.
    high_water:
        Backlog depth (unacked + undelivered) at which ``backpressure()``
        reports that producers should slow down or shed load.
    clock:
        Callable returning epoch milliseconds. Injected for deterministic
        tests; defaults to wall-clock.
    """

    def __init__(
        self,
        name: str,
        *,
        group: str = "workers",
        max_deliveries: int = 5,
        visibility_timeout_ms: int = 60_000,
        maxlen: Optional[int] = None,
        high_water: int = 1_000,
        clock: Callable[[], int] = _wall_clock_ms,
    ) -> None:
        if max_deliveries < 1:
            raise ValueError("max_deliveries must be >= 1")
        self.name = name
        self.group = group
        self.max_deliveries = max_deliveries
        self.visibility_timeout_ms = visibility_timeout_ms
        self.maxlen = maxlen
        self.high_water = high_water
        self._clock = clock

        self._entries: Dict[str, _Entry] = {}
        self._order: List[str] = []  # live stream order, by id
        self._last_delivered_idx = 0  # group cursor over ``_order``
        self._pel: Dict[str, PendingEntry] = {}
        self._dead: List[DeadLetter] = []
        self._seq = 0  # disambiguates ids minted within the same ms

    # -- id minting -------------------------------------------------------

    def _next_id(self) -> str:
        """Mint a Redis-style ``<ms>-<seq>`` id that is strictly increasing."""
        self._seq += 1
        return f"{self._clock()}-{self._seq}"

    # -- producer side: XADD ---------------------------------------------

    def add(self, fields: Dict[str, Any]) -> str:
        """XADD: append a job. Returns its stream id.

        Honours ``maxlen`` by trimming the oldest *delivered-and-acked*
        entries first (real Redis MAXLEN trims oldest regardless; we never
        drop an entry that is still pending so backpressure can never lose
        in-flight work — that is what ``backpressure()`` is for).
        """
        entry_id = self._next_id()
        self._entries[entry_id] = _Entry(entry_id, dict(fields), self._clock())
        self._order.append(entry_id)
        self._trim()
        return entry_id

    def _trim(self) -> None:
        if self.maxlen is None:
            return
        # Drop from the front, but only entries that are no longer live
        # (already acked/removed). Pending or undelivered entries are kept.
        while len(self._order) > self.maxlen:
            head = self._order[0]
            if head in self._entries or head in self._pel:
                break  # oldest live entry is still in flight; stop trimming
            self._order.pop(0)
            if self._last_delivered_idx > 0:
                self._last_delivered_idx -= 1

    # -- consumer side: XREADGROUP ... > ---------------------------------

    def read_group(self, consumer: str, count: int = 1) -> List[Delivery]:
        """XREADGROUP with the ``>`` id: deliver never-before-delivered jobs.

        Each returned entry is recorded in the PEL owned by ``consumer`` with
        ``delivery_count == 1``. Until acked, no other consumer can read it.
        """
        out: List[Delivery] = []
        now = self._clock()
        while len(out) < count and self._last_delivered_idx < len(self._order):
            entry_id = self._order[self._last_delivered_idx]
            self._last_delivered_idx += 1
            entry = self._entries.get(entry_id)
            if entry is None:
                continue  # trimmed/acked between add and read
            self._pel[entry_id] = PendingEntry(
                id=entry_id,
                consumer=consumer,
                delivery_count=1,
                last_delivered_ms=now,
                enqueued_ms=entry.enqueued_ms,
            )
            out.append(Delivery(entry_id, dict(entry.fields), 1))
        return out

    # -- consumer side: XACK ---------------------------------------------

    def ack(self, entry_id: str) -> bool:
        """XACK (+XDEL): job done. Remove it from the PEL and the stream.

        Returns ``False`` if the id was not pending (already acked, or
        reclaimed away from the caller) — the caller should treat its work
        as superseded and not double-write.
        """
        if entry_id not in self._pel:
            return False
        self._pel.pop(entry_id, None)
        self._entries.pop(entry_id, None)
        return True

    # -- consumer side: failure (no server command) ----------------------

    def fail(self, entry_id: str, error: str) -> None:
        """Record a processing failure without acking.

        On Redis this is a no-op against the server: a failed job is simply
        left un-acked so it stays in the PEL and ages toward reclaim. We
        stash ``last_error`` so the eventual dead-letter carries it.
        """
        pe = self._pel.get(entry_id)
        if pe is not None:
            pe.last_error = error

    def defer(self, entry_id: str, *, until_ms: int) -> bool:
        """Keep a pending entry invisible until ``until_ms`` without a delivery.

        Redis visibility is measured from ``last_delivered_ms``. Positioning
        that clock one visibility window before the durable due time makes the
        entry reclaimable at the due time while preserving its delivery count.
        """
        pe = self._pel.get(entry_id)
        if pe is None:
            return False
        pe.last_delivered_ms = int(until_ms) - self.visibility_timeout_ms
        return True

    # -- janitor: XAUTOCLAIM + dead-letter policy ------------------------

    def reclaim(self, consumer: str, count: int = 10) -> List[Delivery]:
        """XAUTOCLAIM stalled entries, dead-lettering the exhausted ones.

        An entry is *stalled* if it has been idle (un-acked) for at least
        ``visibility_timeout_ms``. For each stalled entry, oldest first:

        * if redelivering would push ``delivery_count`` past
          ``max_deliveries``, route it to the dead-letter stream and drop it
          from the live stream;
        * otherwise reassign it to ``consumer``, bump ``delivery_count``,
          reset its idle timer, and return it for reprocessing.

        Returns the entries that were handed to ``consumer`` (the
        dead-lettered ones are not returned — they are terminal).
        """
        now = self._clock()
        out: List[Delivery] = []
        # Oldest pending first, mirroring XAUTOCLAIM's id-ordered scan.
        for entry_id in sorted(self._pel, key=_id_key):
            if len(out) >= count:
                break
            pe = self._pel[entry_id]
            idle = now - pe.last_delivered_ms
            if idle < self.visibility_timeout_ms:
                continue
            if pe.delivery_count >= self.max_deliveries:
                self._dead_letter(entry_id, pe, now)
                continue
            pe.consumer = consumer
            pe.delivery_count += 1
            pe.last_delivered_ms = now
            entry = self._entries[entry_id]
            out.append(Delivery(entry_id, dict(entry.fields), pe.delivery_count))
        return out

    def _dead_letter(self, entry_id: str, pe: PendingEntry, now: int) -> None:
        entry = self._entries.get(entry_id)
        fields = dict(entry.fields) if entry else {}
        self._dead.append(
            DeadLetter(
                dead_id=self._next_id(),
                source_id=entry_id,
                fields=fields,
                delivery_count=pe.delivery_count,
                enqueued_ms=pe.enqueued_ms,
                dead_lettered_ms=now,
                last_error=pe.last_error,
            )
        )
        self._pel.pop(entry_id, None)
        self._entries.pop(entry_id, None)

    # -- introspection: XPENDING / XLEN ----------------------------------

    def pending(self) -> List[PendingEntry]:
        """XPENDING: snapshot of in-flight (delivered, un-acked) entries."""
        return [self._pel[i] for i in sorted(self._pel, key=_id_key)]

    def length(self) -> int:
        """XLEN: number of entries still live in the stream."""
        return len(self._entries)

    def undelivered(self) -> int:
        """Entries added but never delivered to any consumer yet."""
        return max(0, len(self._order) - self._last_delivered_idx)

    def dead_letters(self) -> List[DeadLetter]:
        """XRANGE over the dead-letter stream."""
        return list(self._dead)

    # -- operator: replay from the dead-letter stream --------------------

    def replay(self, dead_id: str) -> Optional[str]:
        """Re-add a dead-lettered job to the live stream for another attempt.

        Returns the new live stream id, or ``None`` if ``dead_id`` is unknown.
        The dead-letter record is consumed (XDEL on the dead stream).
        """
        for i, dl in enumerate(self._dead):
            if dl.dead_id == dead_id:
                self._dead.pop(i)
                return self.add(dl.fields)
        return None

    # -- producer guard: backpressure ------------------------------------

    def backpressure(self) -> Dict[str, Any]:
        """Backlog snapshot and a shed/admit signal for producers.

        ``backlog`` is undelivered + pending work. When it reaches
        ``high_water`` the producer should stop enqueueing new derived jobs
        (or apply a delay) until workers drain it — the equivalent of
        watching XLEN + XPENDING before XADD.
        """
        backlog = self.undelivered() + len(self._pel)
        return {
            "backlog": backlog,
            "high_water": self.high_water,
            "should_shed": backlog >= self.high_water,
            "undelivered": self.undelivered(),
            "pending": len(self._pel),
            "dead": len(self._dead),
        }


def _id_key(entry_id: str) -> tuple[int, int]:
    ms, _, seq = entry_id.partition("-")
    return (int(ms), int(seq or 0))
