# OpenClaw owner-tool protocol - #497 follow-through

`drover.server.harness.openclaw_owner` defines the minimal version-1 owner
interface over `/harness/factory-observer/continuity`. It exports a JSON-schema
tool descriptor (`drover_continuity_owner`), strict Pydantic request/response
types, and `OpenClawOwnerAdapter.invoke`. It has no action executor, scheduler,
messaging transport, automatic retries, implicit acknowledgments, or local
continuity ledger. TaskFlow/Factory retains lifecycle authority.

The standalone [inactive OpenClaw plugin](../plugins/openclaw-continuity-owner/README.md)
now implements normal `api.registerTool` registration, trusted host session/config
binding, and authenticated Drover HTTP. It is not installed or activated. Tests
execute the current NAS 2026.9.6 source entry resolver/registrar with fake registry
bookkeeping and real synthetic authenticated Drover HTTP;
live Gateway discovery, parent messaging, and activation remain unverified.
The linked README records SDK/version evidence, packaging and exact parent canary
steps. No credential, configuration, policy, or service change was made.

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
provides `api.registerTool(...)` for agent-visible tools. The inactive package
binds this schema and execute handler using that path. A
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
execute handler may render its JSON as a normal tool result. The package tests SDK-shaped
result wrapping, actual NAS source entry resolution and registration with fake
registry bookkeeping, not a running
Gateway. Live discovery and authorization remain untested.

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

- The child catalog has no callable `sessions_send`; the user confirms the parent
  has that first-class tool. Child catalog absence is not a core defect.
- The prior missing Drover registration/execute transport is implemented in the
  inactive standalone package. No messaging call or RPC/shell substitution was used.
- Corrected target evidence: NAS OpenClaw 2026.9.6,
  `88027bc85c0a4eebbea49a2a5522faec71ecdc14`, at
  `~/clawd/projects/openclaw`, read through the existing
  `${OPENCLAW_NAS_SOURCE}` mount. Prior Studio 2026.3.13
  proof targeted the wrong checkout and is superseded. Current SDK requires
  manifest `contracts.tools`; repaired registration also uses the V2 live host
  invocation guard. Source locations/runtime limits are in the package README.
- Actual NAS source resolver/registrar with fake registry bookkeeping plus real
  synthetic authenticated Drover HTTP is tested. Actual parent runtime version, registration diagnostics, authorized
  discovery, tool results and normal messaging traces remain the integration gate.

The earlier issue candidate described missing adapter wiring, now supplied as
inactive product code; no core issue is asserted or filed. A real runtime failure
must capture the above diagnostics before core classification. Parent messaging
retains normal authorization/provenance; message receipt never acknowledges the
Drover event. No runtime workaround was installed.

Integration/release stays with
`agent:coder:subagent:1998228b-5eb6-4b11-87a5-339a54576178`.
This worker remains implementation/commit-only. Watchers stay report-only.
Hermes, live approval handoff, publication/review and terminal release remain
untested external integration boundaries.
