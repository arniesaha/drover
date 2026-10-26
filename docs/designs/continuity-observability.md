# Continuity Observability & Stability

*   **Author:** Arnab Saha <arniesaha@gmail.com>
*   **Status:** Proposed
*   **Date:** 2026-10-25
*   **Issue:** [#821](https://github.com/arniesaha/drover/issues/821)
*   **Dependencies:** [#819](https://github.com/arniesaha/drover/issues/819) (Plugin 0.1.4+ and default owner binding)

--------------------------------------------------------------------------------

## Overview

Drover coordinates unattended tasks across personal agent harnesses, autonomous coding workers, and orchestrator sessions. With the launch of the Work coordinator and always-on owner wakes (#819), the continuity layer manages automated worker handoffs, approvals, and owner wakes.

However, operational visibility into this layer is currently fragmented. Stalls and delivery bottlenecks can only be diagnosed after the fact or by manual operator inspection ("status?"). This design provides end-to-end observability and hardening for Drover's continuity subsystem. It introduces a unified continuity status read path (`drover_continuity_status`), strictly attributed alerts naming responsible owners, active awareness of non-hub OpenClaw session lifecycles, and hardened lease and drain lifecycles that survive hub restarts.

### Architectural Context & Terminology

*   **OpenClaw**: The external autonomous agent runtime executing coding and review workers. The OpenClaw Continuity Owner Plugin (`plugins/openclaw-continuity-owner`) runs inside OpenClaw sessions to poll Drover Hub, lease runs, consume wakes, and report worker execution progress.
*   **Wake**: A durable, idempotent outbox record in PostgreSQL (`continuity_wakes`) representing an actionable task event (such as reviewing a worker turn result or investigating a stuck task) requiring agent or operator consumption.
*   **Owner vs. Launcher**:
    *   *Owner*: The specific agent session (e.g., `agent:coder:main`) currently holding an active lease for an issue or run.
    *   *Launcher*: The initiating entity (an operator such as Arnab, or a root coordinator agent) responsible for the run lifecycle.
*   **Idle-Undeclared Worker**: A worker process that has stopped activity and awaits input without cleanly declaring an end-of-turn state contract line (`Drover-State`).
*   **Compare-And-Swap (CAS) Epoch Fence**: A concurrency control mechanism where each lease or consume operation increments a monotonically increasing `owner_epoch`. Operations citing a mismatched epoch are rejected with `ContinuityWakeConflict`, preventing split-brain execution across network partitions or restarts.
*   **Model Context Protocol (MCP)**: The open protocol through which Drover exposes read tools (`drover_continuity_status`) directly to AI assistants.

--------------------------------------------------------------------------------

## The Problem

1. **Invisible Delivery Latency & Backlog**: Continuity wakes flow from the hub outbox (`continuity_wakes`) to owner sessions via poll/consume/acknowledge cycles. When wakes queue up or delivery lags, operators have no single API or dashboard to inspect pending wakes by age, wake-to-consume latency, consume-to-ack latency, or redelivery counts.
2. **Ambiguous Alerts**: Existing stall and timeout alerts frequently emit generic messages or log warnings without assigning clear accountability. When an unconsumed wake or unacknowledged delivery times out, the system must identify the exact actor responsible for taking action rather than reporting that "someone" needs to intervene.
3. **False Stalls for External Owners**: The continuity decision engine (`continuity_loop.py`) evaluates session activity to detect stalls. When an OpenClaw worker session (`agent:*`) is actively executing long-running tasks outside the hub process, the hub's activity detector misses this progress if the session key is not tracked as a first-class activity source. This caused the Oct 25 00:56 incident where active OpenClaw workers were incorrectly marked as stalled.
4. **Restart & Drain Vulnerabilities**: Owner leases in `continuity_owner_leases` and observer runs must reliably maintain continuity across hub service restarts without dropping epochs or spuriously expiring. Furthermore, plugin reload and drain cycles during deployments can wedge if active runs do not gracefully drain within the expected 60-second window.

--------------------------------------------------------------------------------

## Goals

*   **Goal 1: Hub Continuity Metrics & Status API**:
    *   Expose pending wakes partitioned by age buckets (<2m, 2-10m, >10m).
    *   Track and report wake-to-consume latency and consume-to-ack latency percentiles using lightweight in-memory metrics.
    *   Expose active owner leases, current epochs, and lease expirations.
    *   Track idle-undeclared worker detections and outbox redelivery counts.
    *   Record the latest owner-plugin poll status (heartbeat timestamp, consecutive errors, last error, poll duration) per owner session.
    *   Expose all of the above through a single authenticated HTTP route (`GET /continuity/status`) and MCP read tool (`drover_continuity_status`), with Prometheus metrics for the Grafana observatory.
*   **Goal 2: Attributed Alerts with Responsible Owners**:
    *   Alert on wakes unconsumed after 10 minutes (`state = 'pending'`), naming the bound owner session (`agent:*`) or the launcher.
    *   Alert on delivered wakes unacknowledged after 15 minutes (`state = 'delivered'`), naming the consuming owner.
    *   Alert on repeated plugin poll failures (>= 3 consecutive failures within the last hour), naming the owner session and gateway host.
    *   Alert on active runs with no bound owner (> 5 minutes since launch), naming the launcher or system operator.
    *   Strictly enforce that every alert names who acts (no generic or anonymous warnings).
*   **Goal 3: Non-Hub Owner Activity Awareness**:
    *   Recognize `Drover-Session` and `Drover-Owner` keys referencing OpenClaw sessions (`agent:*`) as active if activity occurred within the last 30 minutes.
    *   Incorporate OpenClaw owner activity directly into the hub's pure decision function (`drover.server.continuity_loop.decide`), eliminating dependencies on external NAS reconciler scripts.
*   **Goal 4: Restart-Resilient Leases & Drain Safety**:
    *   Ensure owner lease renewals and CAS epoch fences survive hub restarts without epoch degradation or false cancellations.
    *   Define and enforce a bounded 60-second graceful drain policy for owner plugin reloads so active runs complete or checkpoint cleanly without wedging.

--------------------------------------------------------------------------------

## Non-Goals

*   Mutating or re-architecting the OpenClaw Gateway process or local IPC protocol.
*   Replacing PostgreSQL as the durable control plane store for hub continuity state.
*   Automating merge or public release actions without operator approval (maintaining the strict authority boundary).
*   Building an external distributed tracing platform (Drover records bounded evidence, not arbitrary full-trace telemetry).
*   Executing arbitrary heavy analytical SQL queries against the operational PostgreSQL outbox table.

--------------------------------------------------------------------------------

## Open Questions

*   *Question 1*: How should OpenClaw session activity timestamps reach the hub decision loop?
    *   *Resolution*: The owner plugin reports its active session activity timestamp during its periodic `poll` request via `/harness/factory-observer/continuity`. The hub stores this in `continuity_plugin_heartbeats`. When building facts for `continuity_loop.decide()`, the hub queries active heartbeats for the issue's associated sessions and passes them in `facts["external_session_activity"]`.
*   *Question 2*: How are alerts modeled and delivered?
    *   *Resolution*: Alerts targeting specific issue owners are recorded in `continuity_wakes` with `action="wake"`, `reason="alert"`, and the responsible actor stored in `owner_id` as well as `payload_json["responsible_actor"]`. To prevent recursive alerting loops, alert wakes (`reason="alert"`) are strictly excluded from stuck-wake alert scans. Non-issue system alerts (e.g., repeated poll errors) are exposed via Prometheus gauges and the status API.

--------------------------------------------------------------------------------

## Critical User Journeys (CUJs)

### CUJ 1: Operator Inspects Continuity Health via MCP

*   **Given**: A Drover hub running with active worker jobs and OpenClaw owner sessions.
*   **When**: An operator or orchestrator invokes the MCP tool `drover_continuity_status`.
*   **Then**: Drover returns a structured JSON summary containing pending wake counts grouped by age, average and p95 wake-to-consume and consume-to-ack latencies, active owner leases with remaining TTL and epoch, total redeliveries, idle-undeclared session counts, and recent plugin poll health per session.

### CUJ 2: Unconsumed Wake Triggers Attributed Alert

*   **Given**: An issue wake generated for a completed worker turn with authorized owner `agent:coder:main`.
*   **When**: The wake remains in `pending` state without consumption for 10 minutes.
*   **Then**: The hub alert scanner identifies the threshold violation (>10m) and emits an alert wake explicitly naming `agent:coder:main` as the responsible actor, including the run ID, issue number, and wake age.

### CUJ 3: Active OpenClaw Worker Avoids False Stall

*   **Given**: An issue running a task where the bound owner or worker is an OpenClaw session `agent:coder:subworker-1`.
*   **When**: The session performs internal tool calls for 20 minutes without emitting an intermediate hub-level turn, but reports active turn events via the owner plugin heartbeat.
*   **Then**: The hub `decide()` function evaluates the session activity within the 30-minute window, treats the worker as active, and suppresses false `nudge` or `stall` marker transitions.

### CUJ 4: Hub Restarts During Active Lease

*   **Given**: An OpenClaw owner holding an active lease with epoch 5 valid for 180 seconds.
*   **When**: The Drover hub process restarts.
*   **Then**: The owner issues its next `lease` or `consume` request; the hub reads the durable PostgreSQL state, validates epoch 5, extends the lease, and returns the next delivery without dropping state.

--------------------------------------------------------------------------------

## Technical Plan (Architecture)

```text
 +----------------------------------------------------------------------------+
 |                                Drover Hub                                  |
 |                                                                            |
 |   +------------------------------------+   +---------------------------+   |
 |   |    ContinuityStatusService         |   |   ContinuityLoop (decide) |   |
 |   | - In-memory latency summaries      |   | - Checks OpenClaw agent:* |   |
 |   | - Wake age buckets (<2m, 2-10m, >10m)|   activity within 30m     |   |
 |   | - Active leases, epochs, TTLs      |   | - Pure decision engine    |   |
 |   | - Plugin poll heartbeats & errors  |   +-------------+-------------+   |
 |   +-----------------+------------------+                 |                 |
 |                     |                                    |                 |
 |                     v                                    v                 |
 |   +------------------------------------+        +------------------+       |
 |   | Endpoints & Exporters:             |        |  Alert Sweeper   |       |
 |   | - GET /continuity/status           |        | - Stuck wakes    |       |
 |   | - MCP: drover_continuity_status    |        | - Stale acks     |       |
 |   | - Prometheus /metrics export       |        | - Failing polls  |       |
 |   +-----------------+------------------+        +--------+---------+       |
 |                     |                                    | (Named Owner)   |
 |                     +------------------+                 |                 |
 |                                        |                 v                 |
 |                                        v        +------------------+       |
 |                            +--------------------+ continuity_wakes |       |
 |                            | PostgreSQL Control | (Durable Outbox) |       |
 |                            | Plane Store        +------------------+       |
 |                            +--------------------+                          |
 +----------------------------------------+-----------------------------------+
                                          |
                                          | HTTP / MCP
                                          v
                  +-----------------------------------------------+
                  |             OpenClaw Continuity Owner         |
                  |              Plugin (Version 0.1.6)           |
                  | - Reports session activity & poll errors      |
                  | - Leases, consumes, and acknowledges wakes    |
                  | - Bounded 60s graceful drain on shutdown      |
                  +-----------------------------------------------+
```

### 1. Continuity Metrics & Status Service

The `ContinuityStatusService` provides point-in-time and sliding-window operational metrics:

*   **Pending Wakes by Age**:
    *   Queried efficiently using index scans on `continuity_wakes WHERE state = 'pending'`:
    *   Buckets: `under_2m`, `between_2m_and_10m`, `over_10m`.
*   **Delivery & Acknowledgment Latencies**:
    *   To avoid heavy OLAP sorting queries on PostgreSQL, latencies (`wake_to_consume_ms` and `consume_to_ack_ms`) are tracked via **bounded in-memory rolling summaries** (recording the last 1,000 wake transitions) and exported as Prometheus Histograms (`drover_continuity_latency_seconds{kind="..."}`).
    *   Exposes `avg_ms`, `median_ms`, and `p95_ms` without locking outbox tables.
*   **Active Leases & Epochs**:
    *   Active leases from `continuity_owner_leases`, reporting `issue_key`, `owner_id`, `owner_epoch`, and remaining seconds.
*   **Idle-Undeclared Detections**:
    *   Computed using the existing `worker_states(con)` helper, counting sessions where `worker_state == 'idle_undeclared'`.
*   **Redelivery Counts**:
    *   Count of active wakes with `attempts > 1`.
*   **Owner Plugin Poll Health**:
    *   Durable table `continuity_plugin_heartbeats` tracking each session's last poll timestamp, status (`ok` or `error`), latency, and error message.

#### Sample Response Payload: `GET /continuity/status`

```json
{
  "version": 1,
  "pending_wakes": {
    "total": 3,
    "under_2m": 2,
    "between_2m_and_10m": 1,
    "over_10m": 0
  },
  "latencies": {
    "sample_count": 142,
    "wake_to_consume_ms": { "avg": 1240, "p50": 850, "p95": 4120 },
    "consume_to_ack_ms": { "avg": 8420, "p50": 6200, "p95": 14300 }
  },
  "active_leases": [
    {
      "issue_key": "arniesaha/drover#821",
      "owner_id": "agent:coder:main",
      "owner_epoch": 3,
      "remaining_seconds": 184
    }
  ],
  "idle_undeclared_sessions": 0,
  "redelivered_wakes_count": 1,
  "plugin_heartbeats": [
    {
      "session_key": "agent:coder:main",
      "owner_id": "credential:nas-codex",
      "last_poll_at": "2026-10-25T21:50:12Z",
      "status": "ok",
      "consecutive_failures": 0,
      "latency_ms": 42
    }
  ],
  "active_alerts": []
}
```

### 2. Attributed Alerting Engine

The hub executes periodic alert checks during its reconciliation sweep via `ContinuityAlertSweeper`:

| Condition | Threshold | Responsible Actor | Action | Payload & Routing |
| :--- | :--- | :--- | :--- | :--- |
| Unconsumed wake | `state = 'pending'` for > 10m | Bound `owner_id` (or launcher if unbound) | `wake` (`reason="alert"`) | Wake payload includes `{"alert_type": "stuck_wake", "wake_id": "...", "age_seconds": n}` |
| Unacknowledged delivery | `state = 'delivered'` for > 15m | Consuming `owner_id` | `wake` (`reason="alert"`) | Wake payload includes `{"alert_type": "stale_ack", "wake_id": "...", "elapsed_seconds": n}` |
| Recurring poll failures | >= 3 consecutive failures within 1h | Bound owner session key (`agent:*`) | Prometheus Gauge / Status Alert | Flagged on `/continuity/status` and Prometheus gauge `drover_continuity_poll_failures` |
| Active run without owner | > 5m since launch with null owner | Run launcher / Operator (`arnab`) | Prometheus Gauge / Status Alert | Escalated to operator on `/continuity/status` |

#### Alert Recursion & Deduplication Rules:
1. **Recursion Guard**: Wake rows with `reason = 'alert'` are strictly excluded from stuck-wake and stale-ack scans (`WHERE reason != 'alert'`).
2. **Deterministic Fingerprints**: Alert wake fingerprints are calculated deterministically as `sha256(f"alert:{issue_key}:{alert_type}:{entity_id}")`. Dynamic values like `age_seconds` are kept in payload JSON to prevent duplicate wake insertion across sweeps.

### 3. OpenClaw Owner Activity in Hub Decision Loop

In `drover.server.continuity_loop`:
*   `normalize_facts` accepts `external_session_activity: dict[str, str]`, validating max 32 items, bounded session keys (<=191 chars), and valid ISO timestamps.
*   `_activity(facts)` evaluates timestamps from `external_session_activity` for all associated worker and owner sessions:
    ```python
    session_key = (facts.get("markers") or {}).get("session")
    if session_key and session_key in external_activity:
        stamps.append(_time(external_activity[session_key]))
    ```
*   If an OpenClaw session key was active within `policy.stall_minutes` (default 30 minutes), `_activity()` includes that timestamp, preventing the generation of spurious `nudge` or `stall` decisions.

### 4. Restart Resilience & 60-Second Graceful Drain

*   **Lease Persistence & Epoch Continuity**: Leases in `continuity_owner_leases` store absolute UTC `lease_until` timestamps and monotonically increasing `owner_epoch` values. The hub restart sequence preserves these rows. If an owner's lease expires during hub downtime, the owner may re-acquire the lease, incrementing the epoch. To prevent permanent acknowledgment lockout, `acknowledge()` permits completion if the delivery token matches and the current epoch is `delivery_epoch + 1` from the same `owner_id`, provided no intervening owner claimed the lease.
*   **Graceful Drain Contract**: The OpenClaw plugin hook implements a 60-second shutdown lifecycle via the Node.js `stop()` service lifecycle:
    1. Upon `stop()`, abort the background polling `AbortController`.
    2. Cease consuming new wakes.
    3. Allow in-flight HTTP requests and agent actions to finish up to 55 seconds.
    4. At 55 seconds, if an active lease is held, invoke `lease(lease_seconds=0)` to release the lease cleanly before the process terminates.

--------------------------------------------------------------------------------

## Risks & Mitigations

*   **Risk 1: Database lock contention and memory pressure from status scans**.
    *   *Mitigation*: Latencies are calculated via in-memory bounded circular buffers updated during O(1) transitions. Pending wake queries scan only active rows using partial index `(state, created_at) WHERE state = 'pending'`.
*   **Risk 2: Alert storm cascading through the wake outbox**.
    *   *Mitigation*: Alert wakes are excluded from alert evaluation queries, and alert fingerprints are strictly static (`issue_key:alert_type:entity_id`), guaranteeing exactly one active alert wake per failure condition.
*   **Risk 3: Unbounded growth of plugin heartbeat telemetry**.
    *   *Mitigation*: `continuity_plugin_heartbeats` is keyed by `session_key PRIMARY KEY`, updating in-place on each poll. A daily maintenance sweep purges heartbeats inactive for >7 days.
*   **Risk 4: False progress masking via connectivity-only polling**.
    *   *Mitigation*: The plugin's reported `session_activity_at` must represent the newest agent tool execution or turn activity timestamp, decoupled from the plugin's internal HTTP polling interval.

--------------------------------------------------------------------------------

## Alternatives Considered

### Alternative A: Polling OpenClaw Gateway Over RPC for Activity

*   **Pros**: Hub directly queries Gateway process state.
*   **Cons**: Introduces cross-process network dependencies, fails if Gateway IPC is saturated, and couples the hub decision loop to Gateway daemon uptime.
*   **Reason for Rejection**: Passing heartbeat activity through the owner plugin's existing poll requests decouples the systems and leverages the established authenticated HTTP bridge.

### Alternative B: Independent Alerts via NAS Python Script (`drover.py`)

*   **Pros**: Requires no immediate hub schema changes.
*   **Cons**: Re-fragments decision logic across two implementations, recreating the exact split that #784 was designed to eliminate.
*   **Reason for Rejection**: Drover's core architectural principle is a single pure decision function in the hub.

### Alternative C: On-Demand SQL 24-Hour Percentiles vs In-Memory Summaries

*   **Pros of SQL**: Reflects historical database state across cold restarts.
*   **Cons of SQL**: Heavy CPU sort pressure on OLTP tables, potential lock contention on `continuity_wakes`, and inability to satisfy the 30ms query SLA.
*   **Reason for Rejection**: In-memory rolling summaries updated in O(1) during `consume()` and `acknowledge()` calls provide instant, lock-free metrics while Prometheus handles long-term trend aggregation.

--------------------------------------------------------------------------------

## Non-Functional Requirements

*   **Security & Auth**:
    *   `GET /continuity/status` requires standard Bearer token or Drover session credential.
    *   No raw trace payloads, bearer tokens, or secrets are exposed in status responses or Grafana metrics.
*   **Performance & Latency**:
    *   Status queries execute against indexed PostgreSQL columns with query latency under 30ms.
    *   Prometheus metrics update via bounded in-memory counters and gauges during sweep intervals.
*   **Rollout & Rollback**:
    *   Additive schema migrations (Migration 37).
    *   Rollback consists of deploying the previous binary; database additions remain backward-compatible.

--------------------------------------------------------------------------------

## Detailed Implementation Plan

The implementation is partitioned into seven sequential, buildable phases:

### Phase 1: PostgreSQL & DuckDB Schema (Migration 37)

#### 1. `src/drover/server/postgres_schema.py` (Modify)
*   **Proposed Changes**:
    *   Define `CONTINUITY_OBSERVABILITY_MIGRATION = 37`.
    *   Add Migration 37 DDL:
        ```sql
        CREATE TABLE IF NOT EXISTS continuity_plugin_heartbeats (
          session_key TEXT PRIMARY KEY,
          owner_id TEXT NOT NULL,
          last_poll_at TIMESTAMPTZ NOT NULL,
          status TEXT NOT NULL CHECK (status IN ('ok', 'error')),
          consecutive_failures INT NOT NULL DEFAULT 0,
          last_error TEXT,
          latency_ms INT
        );
        CREATE INDEX IF NOT EXISTS idx_continuity_wakes_pending_age
          ON continuity_wakes (state, created_at)
          WHERE state = 'pending';
        ```
    *   Append migration 37 to `_MIGRATIONS`.

#### 2. `src/drover/server/continuity_wakes.py` & `src/drover/schema.py` (Modify)
*   **Proposed Changes**:
    *   Add `continuity_plugin_heartbeats` table creation to `CONTINUITY_WAKES_DDL` and `bootstrap_control_plane_store` to ensure DuckDB environments mirror the table structure.

*   *Checkpoint 1*: `uv run pytest tests/test_postgres_schema_migrations.py` passes.

---

### Phase 2: Service Layer & Status Projections

#### 3. `src/drover/server/continuity_status.py` (Create)
*   **Proposed Changes**:
    *   Implement `ContinuityStatusService`:
        *   `record_transition(kind: str, latency_ms: int)`: Updates in-memory circular buffer (last 1,000 samples) and computes avg/p50/p95.
        *   `record_plugin_poll(con, session_key, owner_id, status, error, latency_ms)`: Upserts `continuity_plugin_heartbeats`.
        *   `get_status(con, now) -> dict[str, Any]`: Gathers pending wake age buckets, active leases, idle-undeclared session count, and plugin heartbeats.

#### 4. `src/drover/server/continuity_hub.py` (Modify)
*   **Proposed Changes**:
    *   Retain existing `status(self, issue_key: str, ...)` for per-issue queries.
    *   Add `observability_status(self, now: datetime | None = None) -> dict[str, Any]` delegating to `ContinuityStatusService`.

*   *Checkpoint 2*: Unit test `ContinuityStatusService` with simulated transitions and assertions on latency percentiles.

---

### Phase 3: Hub Decision Loop & Activity Integration

#### 5. `src/drover/server/continuity_loop.py` (Modify)
*   **Proposed Changes**:
    *   Update `normalize_facts` to accept and validate `external_session_activity: dict[str, str]` (bounded to 32 keys, <= 191 chars per key, valid ISO timestamps).
    *   Update `_activity(facts)` to check `markers.session` and bound owners against `external_session_activity` and append active timestamps within the 30m window.

*   *Checkpoint 3*: `uv run pytest tests/test_continuity_loop.py` passes with test verifying OpenClaw session activity prevents false stalls.

---

### Phase 4: Attributed Outbox Alert Sweeper

#### 6. `src/drover/server/continuity_sweeper.py` (Create)
*   **Proposed Changes**:
    *   Implement `ContinuityAlertSweeper`:
        *   `sweep_alerts(con, now)`: Scans for:
            1. Unconsumed wakes (`state = 'pending' AND created_at <= now - interval '10 minutes' AND reason != 'alert'`).
            2. Stale acks (`state = 'delivered' AND delivered_at <= now - interval '15 minutes' AND reason != 'alert'`).
        *   Enqueues wake rows with `action="wake"`, `reason="alert"`, `owner_id=responsible_actor`, and payload `{"alert_type": "...", "responsible_actor": "..."}` using static deduplication fingerprints.

*   *Checkpoint 4*: Unit test alert sweeper asserting wake attribution and recursion exclusion.

---

### Phase 5: HTTP Endpoints, MCP Tool, and Prometheus Exporter

#### 7. `src/drover/server/web/app.py` & `src/drover/server/web/auth.py` (Modify)
*   **Proposed Changes**:
    *   Mount `GET /continuity/status` in `RequestHandler`, requiring authenticated session or bearer token.
    *   Update `_factory_observer_continuity()` query parameter allowlist to accept optional `session_activity_at` and `last_error`.

#### 8. `src/drover/server/mcp/tools.py` & `src/drover/server/mcp/server.py` (Modify)
*   **Proposed Changes**:
    *   Implement `drover_continuity_status` read tool returning status JSON.
    *   Register `@mcp.tool()` on FastMCP server.

#### 9. `src/drover/server/metrics.py` (Modify)
*   **Proposed Changes**:
    *   In `MetricsCollector.collect()`, register and export:
        *   `drover_continuity_pending_wakes{age_bucket="under_2m|2m_to_10m|over_10m"}`
        *   `drover_continuity_latency_seconds{kind="wake_to_consume|consume_to_ack",quantile="0.5|0.95"}`
        *   `drover_continuity_stuck_wakes`
        *   `drover_continuity_poll_failures{session_key="..."}`

*   *Checkpoint 5*: Integration test calling `GET /continuity/status` and invoking MCP tool `drover_continuity_status`.

---

### Phase 6: OpenClaw Plugin Activity Reporting & Bounded Drain

#### 10. `plugins/openclaw-continuity-owner/index.js` & schemas (Modify)
*   **Proposed Changes**:
    *   Update `schemas/request.json` to allow optional `session_activity_at` and `last_error` in `Poll` requests.
    *   Include latest agent activity timestamp in periodic poll request.
    *   Implement `stop()` hook: cancel polling controller, wait up to 55s for in-flight requests, and release active leases before exiting.
    *   Bump plugin version to `0.1.6` in `package.json` and `openclaw.plugin.json`.

*   *Checkpoint 6*: `npm test` in `plugins/openclaw-continuity-owner/` passes.

---

### Phase 7: Grafana Dashboard Observatory

#### 11. `deploy/grafana/drover-pipeline-observatory.json` (Modify)
*   **Proposed Changes**:
    *   Add "Continuity Health & Latency" row with panels for:
        *   Pending Wakes by Age (Stacked Bar).
        *   Wake-to-Consume & Consume-to-Ack Latency (Time Series).
        *   Active Owner Leases & Epochs (Stat/Table).
        *   Attributed Continuity Alerts (Stat with thresholds).

*   *Checkpoint 7*: Validate JSON syntax and datasource bindings.

---

### Verification Plan

*   **Unit Tests**:
    ```sh
    uv run pytest tests/test_continuity_observability.py tests/test_continuity_hub.py tests/test_continuity_loop.py tests/test_continuity_wakes.py tests/test_postgres_schema_migrations.py
    ```
*   **Test Cases to Implement in `tests/test_continuity_observability.py`**:
    *   `test_status_empty_store`: Returns valid zeroed metrics and empty lists when outbox has no records.
    *   `test_status_latency_percentiles`: Asserts accurate p50 and p95 calculations over recorded transitions.
    *   `test_alert_unconsumed_wake_attribution`: Emits wake naming the responsible owner when a wake is unconsumed for >10m.
    *   `test_alert_stale_ack_attribution`: Emits wake naming the consuming owner when a delivery is unacknowledged for >15m.
    *   `test_alert_recursion_prevented`: Verifies alert wakes are never flagged as stuck wakes.
    *   `test_openclaw_session_activity_prevents_false_stall`: Verifies `decide()` returns `action="none"` when `external_session_activity` contains activity within 30m.
    *   `test_lease_renewal_survives_hub_restart`: Verifies CAS epoch fence continuity across simulated store reconnection.
*   **Plugin Tests**:
    ```sh
    cd plugins/openclaw-continuity-owner && npm test
    ```

--------------------------------------------------------------------------------

*Drafted and verified with EGM*
