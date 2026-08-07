# Structured Session Start Sequence Design

Date: 2026-08-06
Status: Approved

## Problem

New structured sessions persist `session.started` before starting their driver, but that event is currently written with a null `seq`. `StructuredSessionManager.start()` then initializes its counter from `HarnessRegistry.max_event_seq()` and assigns sequence 1 to the first driver message. Every active structured session therefore mixes one unsequenced event with sequenced messages, violating the stage 1 invariant that a session is either fully legacy-unsequenced or fully sequenced.

## Goals

- Give every newly created structured session a contiguous sequence beginning with `session.started` at sequence 1.
- Preserve the current ordering that records the running state and start event before the structured driver can emit messages.
- Keep PTY session behavior and all other lifecycle event behavior unchanged.
- Add a regression test that proves a structured session never contains a null sequence and that its sequences are contiguous.

## Design

Extend `_safe_append_event` with an optional `seq` argument and forward it unchanged to `HarnessRegistry.append_event`. In `_create_structured_session`, pass `seq=1` only when writing the initial `session.started` event.

The structured manager already reads `max_event_seq(session_id)` before constructing its driver. With the start event stored at sequence 1, the manager initializes its counter to 1 and assigns sequence 2 to the first driver-emitted message. Its existing per-session lock continues to serialize all later sequence allocation.

This is preferable to removing `session.started`, which would discard useful lifecycle metadata, or moving start-event persistence into `StructuredSessionManager`, which would broaden the manager's responsibilities and require a larger concurrency-sensitive refactor.

## Error Handling

The existing best-effort behavior remains unchanged. `_safe_append_event` still returns `None` if the registry write fails, and structured driver startup remains available even when local audit persistence is unavailable. No retry or deployment behavior changes are included.

## Testing

Update the existing structured-session lifecycle test to read all locally persisted events after the fake driver reaches its input state. Assert that:

- the first event is `session.started` with `seq == 1`;
- every event has a non-null sequence; and
- the ordered sequence list is exactly `1..N`.

The test must fail on the current implementation because `session.started.seq` is null. After the minimal implementation, run the focused daemon test and the affected harness test suite.

## Non-Goals

- Repairing or mutating deployed databases.
- Restarting or changing any service or launch agent.
- Implementing server pagination, gzip, or the iOS SQLite cache.
- Changing PTY event sequencing.
- Deploying or pushing the fix.

## Acceptance Criteria

- A newly created structured session stores `session.started` at sequence 1.
- All subsequent structured events have unique contiguous sequences beginning at 2.
- No new structured session contributes to the mixed-sequence-session metric.
- Existing affected tests pass without service, database, or deployment operations.
