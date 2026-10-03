# OpenClaw owner-tool protocol — #497 follow-through

`drover.server.harness.openclaw_owner` defines the minimal version-1 owner
interface over `/harness/factory-observer/continuity`. It exports a JSON-schema
tool descriptor (`drover_continuity_owner`), strict Pydantic request/response
types, and `OpenClawOwnerAdapter.invoke`. It has no action executor, scheduler,
messaging transport, automatic retries, implicit acknowledgments, or local
continuity ledger. TaskFlow/Factory retains lifecycle authority.

This is a **contract and mock integration proof**, not a deployed OpenClaw
plugin. The descriptor alone does not make a tool available to a parent.
An actual plugin execute handler and normally authenticated Drover transport
must be supplied through supported OpenClaw integration, under existing scopes.
No registration, credential, configuration, policy, or service change was made.

## Trusted binding and normal invocation

An integration must initialize the existing observer run through the existing
continuity endpoint first. It then binds the adapter's `run_id`, `owner_id`
(the trusted owner session identity), and `authority_scope` outside model input.
The tool request cannot choose another run, impersonate an owner, broaden scope,
grant approval, or supply a URL, token, command, or arbitrary endpoint path.
All transport calls use the fixed existing continuity endpoint. The injected
`ContinuityEndpoint.get/post` transport must enforce ordinary Drover authentication;
the Python protocol neither implements nor bypasses authentication.
Before a mutation, a bounded GET checks the run's immutable scope against the
trusted binding. A wrong scope is refused before any POST, including ack.

[OpenClaw's supported plugin API](https://docs.openclaw.ai/plugins/sdk-overview/tools-and-commands)
provides `api.registerTool(...)` for agent-visible tools. A runtime integration
must bind this descriptor/schema and an execute handler using that path. A
parent can then explicitly invoke the normally available tool, or use authorized
[normal session messaging](https://docs.openclaw.ai/concepts/session-tool) to ask
the bound owner to invoke it. Message receipt is data, not a Drover acknowledgment.
This module never sends a message. Shell messaging and direct Gateway RPC are
not part of the protocol.

Every tool call uses this envelope:

```json
{
  "version": 1,
  "request": {"operation": "consume", "owner_epoch": 1}
}
```

| operation | Request fields | Boundary operation |
| --- | --- | --- |
| `poll` | optional `limit` (1–50, default 20) | GET status for the bound run |
| `lease` | optional `owner_epoch`, `lease_seconds` (1–300, default 60) | lease/CAS renewal for the bound owner |
| `report` | `source_event_id`, `subject`, `sequence`, `kind`, `summary` | immutable report with source `openclaw` |
| `consume` | `owner_epoch` | commit and return one advisory owner action |
| `acknowledge` | `owner_epoch`, `event_id`, `checkpoint` | atomically acknowledge and checkpoint owner continuation |

Unknown fields/operations/versions and coerced epoch values (strings or booleans)
are rejected before transport. Replies validate run/scope, TaskFlow authority,
scope limits, event/action identities, bounded inbox fields, allowed action
vocabulary, and the current owner fence for owner mutations. Unsupported action
names such as `merge` fail validation. A response validation failure can follow
a committed endpoint call; the owner must poll to resolve the outcome.

`OwnerToolReply` contains `version: 1`, the typed `continuity` projection,
optional `event_id` for reports, and optional `delivery` for consume. An OpenClaw
execute handler may render its JSON as a normal tool result. Its runtime-specific
result wrapping, tool discovery, authorization, and registration are not tested
by the mock harness.

## Explicit delivery, reconciliation, and ack

The owner keeps the returned lease epoch and renews with it. Worker completion
becomes a durable report and then `review_worker_result`; it does not release
the run. CI green becomes `review_ci`, never merge. CI red becomes `correct_ci`.
The owner explicitly reconciles the action in its allowed scope and explicitly
acks with the event ID and updated checkpoint. Initialization, schema generation,
polling, receipt of a message/tool reply, and tool-result rendering do not ack.

Implementation remains commit-only; integration remains review/publish;
deployment produces an unauthorized `request_explicit_approval` action and the
endpoint refuses its ack. A label does not grant worker tools or approval.
The adapter never performs the proposed action. Terminal release remains
external and is never inferred from `worker_brief_completed` or CI green.

On delivery/transport failure, no automatic follow-up call is made. Consume may
have committed before its reply was lost. Poll reconstructs the outstanding
action; retrying consume respects the existing three-delivery budget and
5/10/20-second backoff. The owner must reconcile any prior side effect using the
stable `action_id` before acknowledging. Duplicate report/ack retries retain
the immutable event identity and cannot overwrite a later checkpoint. This is
at-least-once delivery with idempotent consumption, not exactly once.

`tests/test_openclaw_owner_protocol.py` explicitly names its in-process mock
normal-tool harness. Its injected endpoint calls the real durable continuity
boundary/store, reconstructed on every call. It proves completion → event →
authorized action → explicit ack → reconstructed adapter/store → no duplicate
delivery; both CI colors; a reply lost after committed consume retaining the
unacknowledged action; and recovery without implicit ack. It also checks trusted
scope/identity rejection and approval blocking. This is not evidence of a live
OpenClaw session, SDK plugin registration, authenticated runtime transport, or
parent-message delivery. PostgreSQL proof is in its separate test module.

## Evidence for the OpenClaw integration/core handoff

Observed in worker `harness-f5032b0f-837b-4769-8fcd-f39fb48d6281` on 2026-10-03:

- The exposed tool catalog contained no callable OpenClaw messaging/session tool.
  No runtime messaging call was attempted, and no shell/RPC substitution was used.
- Drover had OpenClaw collection/provenance support, but no registration or execute
  handler for this owner tool. Searches of `src`, `scripts`, and `docs` found no
  `registerTool`/`sessions_send` owner integration before this slice.
- Official OpenClaw documentation describes normal tool registration and session
  messaging. Thus missing wiring in this session is **not proof of a core defect**.
  The running parent's OpenClaw version, tool catalog/policy, and plugin-loading
  diagnostics were not available or inspected.

Issue candidate: **Expose the Drover continuity owner contract through the normal
OpenClaw parent/owner tool surface and verify durable delivery/ack recovery.**
Attach this protocol and the mock/PG proofs. Before classifying a core defect,
capture the actual runtime version, registration diagnostics, authorized parent
tool discovery/result, and failed normal invocation. Acceptance should prove the
same completion/CI/failure cases through a real normally registered tool, without
direct RPC/shell messaging, permission bypass, or implicit ack. Parent messaging
must preserve its normal authorization and inter-session provenance. No issue
was filed and no runtime workaround was installed.

Integration/release stays with
`agent:coder:subagent:1998228b-5eb6-4b11-87a5-339a54576178`.
This worker remains implementation/commit-only. Watchers stay report-only.
Hermes, approval handoff, publication/review, and terminal release are untested
external integration boundaries.
