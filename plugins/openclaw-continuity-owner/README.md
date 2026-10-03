# Drover continuity owner plugin (inactive by default)

This standalone ESM package registers exactly `drover_continuity_owner` through
normal `api.registerTool(factory, {name, optional: true})`. Its execute handler
uses authenticated HTTP at the fixed `/harness/factory-observer/continuity`
endpoint. It has no messaging client, executor, scheduler, implicit ack, or
automatic retry. Installing/loading/activating it is an integration-owner task;
none was performed by this implementation worker.

## SDK evidence and limits

Inspected local source: `/Users/arnab/Developer/research/openclaw`, version
**2026.3.13**, commit `421effcf905b0956895166316c3fbe62baf6a22f`.
The requested `projects/openclaw` checkout was absent at the searched local
roots. This source was read without modification.

- `src/plugins/types.ts:63–89`: trusted tool context `sessionKey`, factory
  returning a tool or null, and optional registration options.
- `src/plugins/types.ts:366–380`: `pluginConfig` and `registerTool` API.
- `src/plugins/registry.ts:194–218`: normal factory registration normalization.
- `extensions/llm-task/index.ts`: default function registration example.
- `docs/plugins/agent-tools.md`: raw JSON-schema parameters and optional tools
  subject to normal tool availability/policy.
- `docs/tools/plugin.md` and `src/plugins/install.ts`: manifest/package loading
  and tarball installation. **CLI install changes configuration and enables an
  entry**; do not treat install as inactive staging without authorization.

The checkout lacks installed SDK dependencies/build output. Tests therefore use
a small source-matched registration host, not a running Gateway. A downloaded
OpenClaw app reports 2026.9.6; that runtime was not tested. The integration owner
must verify the actual parent's SDK/version, schema normalization, discovery,
existing policy, and execute/result behavior before activation. The child lacks
`sessions_send`; the user confirms the parent has
first-class `sessions_send`. This is not evidence of a core defect.

## Trusted configuration

The manifest ID is `drover-continuity-owner` (distinct from the npm package name).
Both an absent configuration and `config.enabled: false` return no tool from the
factory. Default mode is `poll_only`. A configured parent canary binding is
always read-only, including in owner mode, and must differ from the owner key.
Unbound/missing host session keys receive no tool. Exactly one owner binding is
supported; the store's lease/epoch remains the authority for mutations.

Example **inactive** host configuration (placeholders, not instructions to edit
this worker's configuration):

```json
{
  "plugins": {
    "entries": {
      "drover-continuity-owner": {
        "enabled": false,
        "config": {
          "enabled": false,
          "mode": "poll_only",
          "droverOrigin": "https://drover.example",
          "tokenEnvName": "DROVER_CONTINUITY_OWNER_TOKEN",
          "runId": "run_CANARY",
          "ownerSessionKey": "agent:coder:existing-authorized-owner",
          "canarySessionKey": "agent:main:existing-authorized-parent",
          "authorityScope": "implementation",
          "timeoutMs": 5000
        }
      }
    }
  }
}
```

Use a preexisting approved bearer credential environment variable and approved
Drover origin; no model-supplied credential, URL, command, owner, scope, or run
is accepted. HTTPS or loopback HTTP is required; origins cannot contain paths,
userinfo, query, or fragment. Requests reject redirects, use a maximum 10-second
timeout, cap replies at 2 MiB, and sanitize transport errors. Config is strict;
no arbitrary extra keys. All payloads/replies use exported existing Python
protocol schemas and semantic authority/fencing checks. JS-unsafe integer epochs
or sequences are refused rather than rounded. Mutations first check the run's
immutable scope by GET. Ordinary server authentication is never bypassed.

The optional tool remains subject to existing OpenClaw tool policy. Do not widen
permissions or enable all plugin tools to make a canary pass. Filtered discovery
is a handoff diagnostic, not authorization to bypass policy.

## Build and focused proof

From the Drover root:

```sh
PYTHONPATH=src .venv/bin/python scripts/export_openclaw_owner_schema.py --check
.venv/bin/python -m pytest tests/test_openclaw_owner_plugin.py tests/test_openclaw_owner_protocol.py -q -rs
```

From this package directory:

```sh
npm ci --ignore-scripts --no-audit --no-fund
DROVER_TEST_OPENCLAW_SOURCE=/Users/arnab/Developer/research/openclaw npm test
npm pack --ignore-scripts
```

The SDK source-shape test explicitly skips when that checkout is unavailable.
The Python tests skip Node execution when Node/dependencies are missing. They
exercise real authenticated Drover HTTP with synthetic credentials, a durable
local store and reconstructed store, but a fake SDK host. They prove read-only
parent/owner polling, explicit owner lease/consume/ack, post-reconstruction no
duplicate delivery, and 401 refusal. Protocol tests prove CI green → `review_ci`,
CI red → `correct_ci`, blocked deployment and lost-delivery recovery. Node tests
also cover normal registration shape, session isolation, strict requests,
transport refusal/redirect/cancellation, scope preflight and no automatic ack.
Neither these tests nor the package establish live OpenClaw delivery or new
PostgreSQL proof. Prior PG evidence/gaps are recorded in the Drover ledger.

## Parent canary procedure — requires integration review and authorization

Owner for integration/release:
`agent:coder:subagent:1998228b-5eb6-4b11-87a5-339a54576178`.
The worker does not perform any step below against a live runtime.

1. Review the commit and packed artifact; verify the parent's supported SDK.
   With explicit authorization for installation/configuration/loading, the normal
   local SDK command is `openclaw plugins install ./drover-openclaw-continuity-owner-0.1.0.tgz`.
   That command changes configuration. Coordinate inactive staging and approved
   activation through the host's supported procedures; retain the package's
   `config.enabled: false` until the canary is approved. Any necessary host reload
   or restart is separately owned and authorized, never performed by this worker.
2. Select an **existing isolated test Factory observer session**; initialize its
   continuity ledger through the existing authenticated Drover endpoint. This
   tool deliberately does not initialize or launch sessions. Initialization body:
   `{"operation":"initialize","session_id":"<existing test session>","objective":"#497 advisory canary","checkpoint":"Canary awaiting read-only receipt","authority_scope":"implementation"}`.
   Use the returned `continuity.run_id`. POST one advisory report to the same
   endpoint with `operation: "report"`, that `run_id`, `source: "openclaw"`, a
   unique `source_event_id`, `subject: "canary"`, `sequence: 1`,
   `kind: "worker_brief_completed"`, and `summary: "Synthetic advisory canary; no action execution"`.
   Record its event ID. Do not use a production run or release a worker.
3. Through authorized normal plugin configuration/loading, bind that run,
   implementation scope, existing owner and distinct parent session keys, approved
   transport and token variable. Enable the plugin with `mode: "poll_only"`.
   Confirm normal tool availability under existing policy. Parent invokes
   `drover_continuity_owner` with:
   `{"version":1,"request":{"operation":"poll","limit":1}}`.
   Verify TaskFlow authority, bound run/scope, pending event and no terminal release.
4. Parent may invoke its existing **first-class `sessions_send`** targeting that
   existing owner session, asking it to invoke the same read-only poll envelope
   and return the observed event ID/run ID. Use the parent's normal supported
   messaging tool and permissions; this adapter does not call it. Message text
   must request poll only, no consume/ack/execution. No new worker is launched.
5. Parent verifies its poll and the owner's normal tool/message receipt match.
   This is a receipt check, **not a durable acknowledgment**. Missing/failed
   delivery leaves the event pending; retain traces and poll for recovery.
6. Only after verified receipts and explicit permission for owner mutations,
   integration owner may authorize `mode: "owner"` activation. The parent canary
   key remains read-only. Owner explicitly invokes `lease` (e.g. 60 seconds),
   then `consume` using the returned live epoch. Verify `review_worker_result`
   and stable action/event IDs. Reconcile only the synthetic advisory in the
   permitted scope, then explicitly invoke
   `{"version":1,"request":{"operation":"acknowledge","owner_epoch":<returned epoch>,"event_id":"<recorded SHA256>","checkpoint":"Synthetic canary reconciled; no release"}}`.
   Poll confirms acknowledgment and no next action; reconstruct/retry confirms
   no duplicate delivery. Real runtime restart recovery requires separately
   authorized host operations, not this worker's tests.

If a mutation reply is lost, poll before retrying: the mutation may have committed.
Use stable action/event identities for reconciliation, never infer ack from a
message, and respect existing bounded delivery retry/lease recovery semantics.
Delivery is at-least-once with idempotent consumption, not exactly-once.
Implementation is commit-only; integration is review/publish; deployment needs
explicit approval. CI green proposes review, never merge. Worker completion does
not infer terminal release. Watchers remain report-only. Hermes is untested.

No core issue is asserted. A real failure report must include parent runtime
version, registration/loading diagnostics, existing authorized tool discovery,
normal invocation/result and session-message traces with secrets removed. Child
catalog absence alone does not qualify.
