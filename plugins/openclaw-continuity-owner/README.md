# Drover continuity owner plugin (inactive by default)

This standalone ESM package registers exactly `drover_continuity_owner` through
normal `api.registerTool({contextVersion: 2, create}, {name, optional: true})`.
Its execute handler uses authenticated HTTP at the fixed `/harness/factory-observer/continuity`
endpoint. In explicitly approved `owner` mode, a plugin-owned service maintains the
fenced Drover owner lease and translates `owner_wake` into a system event plus an
immediate heartbeat for the exact trusted owner session. The durable Drover event
remains the retry source across reload/reconnect; the service never consumes,
executes, acknowledges, merges, deploys, or infers terminal release. `poll_only`
and deployment-scope bindings keep the reload-safe service registration inert.
Installing, loading, and activating the plugin remain integration-owner operations.

The explicit V2 factory is paired with the static authoring metadata symbol
used by OpenClaw's `plugins build`, `validate`, and `pack` commands. The
metadata is required for reviewable artifact creation; a plain default plugin
definition can load at runtime but cannot be packed by OpenClaw 2026.9.9. Runtime
schemas and manifest metadata are statically imported so the packed bundle has no
module-relative filesystem reads.

## SDK evidence and limits

Current target source: **NAS OpenClaw 2026.9.9**, commit
`70f61827e0be4c1f099369c4947c1e7740c60e42`, at
`~/clawd/projects/openclaw`. This worker reads it through the existing
SMB mount `${OPENCLAW_NAS_SOURCE}`
(host-specific mount details omitted). The checkout is read-only to this task.
The prior Studio 2026.3.13 source proof targeted the wrong checkout and is
superseded; it does not establish NAS compatibility.

Current-source evidence:

- `package.json:1454–1456,1612–1614`: exported `openclaw/plugin-sdk/plugin-entry`
  and `openclaw/plugin-sdk/tool-plugin` runtime/type subpaths.
  `src/plugin-sdk/plugin-entry.ts` exports `definePluginEntry` and the tool/API
  types. No SDK runtime import is needed for our supported plain definition object.
- `src/plugins/plugin-definition.types.ts` and `src/plugins/module-export.ts`:
  default definition object with synchronous `register(api)` is accepted. The
  actual source resolver is executed in the focused contract test.
- `src/plugins/plugin-api.types.ts:194,214–217` and `tool-types.ts`: host
  `pluginConfig`, trusted `sessionKey`, normal optional registration, V2 factory
  descriptor and required `assertInvocationCurrent` callback.
- `src/plugins/registry-registrars-tools-hooks.ts:207–272`: registrations **must
  declare `contracts.tools`**; V2 descriptors are normalized through `create`
  and require host invocation authority. The old manifest lacked this declaration
  and would be rejected. The test executes this actual source body and proves
  rejection of the old manifest shape as well as registration of the repaired one.
- `docs/plugins/manifest/capabilities.md`: `contracts.tools`, optional/side-effect
  metadata and `activation.onStartup`; `src/plugins/manifest.ts:233,306,379`
  parses these declarations. Manifest now declares the one owned tool, default
  disabled, startup lazy, optional and side-effecting (never replay-safe).
- `docs/plugins/tool-plugins.md:183–217`: V2 live authority must be checked after
  awaits in the final synchronous request guard. We check immediately before
  each GET/POST, including POST after scope preflight. Metadata discovery grants
  no invocation authority. Drover lease/epoch and scope checks still apply.
- `docs/plugins/sdk-entrypoints/package-entries.md`: source/runtime entries pair
  positionally. Both entries explicitly select the shipped `index.js` ESM artifact.
- `docs/plugins/manage-plugins.md:236–237` documents
  `openclaw plugins install npm-pack:<path.tgz>`;
  `src/plugins/install-source-plan.ts:122–132` resolves that supported archive
  form. Installation/configuration/reload are administrator actions and require
  explicit user authorization; installation is not read-only staging.
- `package.json:2370–2372` and `docs/plugins/tool-plugins.md`: supported Node range
  `>=24.16.0 <25 || >=26.1.0`, now mirrored by the package engine declaration.
- Installed OpenClaw **2026.9.9** (`157d653`): `plugins pack` calls
  `getToolPluginMetadata` before it builds the review artifact. The entry now
  exposes the same non-enumerable `Symbol.for("openclaw.plugin-sdk.tool-plugin.metadata")`
  contract used by `defineToolPlugin`, while preserving the required V2 factory
  for trusted session and live-invocation authority.

`test/nas-source.test.js` requires `DROVER_TEST_OPENCLAW_NAS_SOURCE` and rejects
versions other than 2026.9.9. It imports the actual pure source entry resolver
and contract helpers and executes the actual registrar body with fake surrounding
registry bookkeeping. It does not load the whole Gateway or SDK lifecycle.
The remaining tool tests use a minimal V2 fake matching that tested registrar.
Current NAS source has dependencies/build output; this proof deliberately loads
only these bounded source helpers, without plugin installation or host activation.
Live tool discovery, schema normalization, host lifetime/continuation behavior,
normal authenticated transport in the Gateway and exact-session heartbeat delivery
remain live-acceptance gates. Focused tests cover service cancellation, lease fencing,
bounded retry, reconnect replay before acknowledgment, and suppression after durable
acknowledgment.

## Trusted configuration

The manifest ID is `drover-continuity-owner` (distinct from the npm package name).
Both an absent configuration and `config.enabled: false` return no tool from the
factory. Default mode is `poll_only`. A configured parent canary binding is
always read-only, including in owner mode, and must differ from the owner key.
Unbound/missing host session keys receive no tool. Exactly one owner binding is
supported; the store's lease/epoch remains the authority for mutations. A stable,
reload-safe service registration reads committed config on every start and runs
only for an enabled non-deployment `owner` binding. It renews only the configured
principal's lease, refuses a competing live owner, emits only to a canonical
`ownerSessionKey`, and retries one event no faster than `wakeRetryIntervalMs`.

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
          "ownerPrincipal": "credential:existing-drover-credential-id",
          "canarySessionKey": "agent:main:existing-authorized-parent",
          "authorityScope": "implementation",
          "timeoutMs": 5000,
          "wakePollIntervalMs": 15000,
          "wakeRetryIntervalMs": 60000,
          "ownerLeaseSeconds": 60
        }
      }
    }
  }
}
```

Use a preexisting approved bearer credential environment variable and approved
Drover origin. `ownerSessionKey` is the trusted OpenClaw `agent:` session that
may receive the tool; `ownerPrincipal` is the server-derived principal for its
bearer (`credential:<credential-id>`, or `legacy:operator` only for the
supported legacy bearer). They are deliberately distinct, so a model cannot
turn an OpenClaw session key into an HTTP owner claim. No model-supplied
credential, URL, command, owner, scope, or run is accepted. Trusted
initialization sends its configured immutable scope, so integration and
deployment handoffs retain their scope. HTTPS or loopback
HTTP is required; origins cannot contain paths,
userinfo, query, or fragment. Requests reject redirects, use a maximum 10-second
timeout, cap replies at 2 MiB, and sanitize transport errors. Config is strict;
no arbitrary extra keys. All payloads/replies use exported existing Python
protocol schemas and semantic authority/fencing checks. JS-unsafe integer epochs
or sequences are refused rather than rounded. Mutations other than first
initialization check the run's immutable scope by GET; trusted first
initialization posts its fixed binding and validates the bound reply after token
redemption. Ordinary server authentication is never bypassed.

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
npm ci --no-audit --no-fund
DROVER_TEST_OPENCLAW_NAS_SOURCE="$OPENCLAW_NAS_SOURCE" npm test
openclaw plugins validate --root . --json
openclaw plugins pack --root . --out /tmp/drover-openclaw-continuity-owner.tgz --json
```

The current-source test fails explicitly if the NAS path/environment is missing or
points at another version; it cannot silently substitute the old Studio source.
The Python tests skip Node execution when compatible Node/dependencies are missing. They
exercise real authenticated Drover HTTP with synthetic credentials, a durable
local store and reconstructed store, but a fake SDK host. They prove read-only
parent/owner polling, explicit owner lease/consume/ack, service-owned lease renewal,
exact-session wake routing, competing-owner refusal, bounded reconnect retry,
post-ack suppression, post-reconstruction no duplicate delivery, and 401 refusal. Protocol tests prove CI green → `review_ci`,
CI red → `correct_ci`, blocked deployment and lost-delivery recovery. Node tests
also cover current source load/registration, session isolation, strict requests,
transport refusal/redirect/cancellation, scope preflight, live authority revoked
during preflight preventing POST, and no automatic ack.
Neither these tests nor the package establish live OpenClaw delivery or new
PostgreSQL proof. Prior PG evidence/gaps are recorded in the Drover ledger.

## Parent canary procedure - requires integration review and authorization

Owner for integration/release:
`agent:coder:subagent:1998228b-5eb6-4b11-87a5-339a54576178`.
The worker does not perform any step below against a live runtime.

1. Review the commit and packed artifact; verify the parent's supported SDK.
   Only with explicit user authorization for installation/configuration/loading, the current-source-supported
   owner command is `openclaw plugins install npm-pack:/<approved-artifact-directory>/drover-openclaw-continuity-owner-0.1.0.tgz`.
   That command changes configuration. Coordinate inactive staging and approved
   activation through the host's supported procedures; retain the package's
   `config.enabled: false` until the canary is approved. Any necessary host reload
   or restart is separately owned and authorized, never performed by this worker.
2. Select an **existing isolated test Factory observer session**. Its bound host
   credential (or the legacy operator bearer) first issues a one-time
   `issue_handoff` for the intended `credential:<owner-id>`; that owner then
   explicitly invokes the plugin's trusted-config `initialize` action with the
   returned handoff token. The plugin deliberately does not issue handoffs or
   launch sessions. Poll never initializes or acknowledges implicitly.
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
4. Only after the read-only receipt and explicit permission for owner mutations,
   authorize `mode: "owner"` activation. The parent canary key remains read-only.
   The service acquires/renews the configured owner lease and, when the durable
   projection says `owner_wake`, queues a system event and immediate heartbeat to
   exactly `ownerSessionKey`. Capture the service wake and exact-session turn trace;
   no parent `sessions_send` nudge is permitted in this acceptance.
5. The awakened owner polls, consumes using the returned live epoch, verifies the
   stable `review_worker_result` action/event IDs, and advances only the permitted
   scope. The plugin service is not an executor and cannot perform this step.
6. Only after durable advancement, the owner explicitly invokes
   `{"version":1,"request":{"operation":"acknowledge","owner_epoch":<returned epoch>,"event_id":"<recorded SHA256>","checkpoint":"Synthetic canary reconciled; no release"}}`.
   Poll confirms acknowledgment and no next action. Stop/start the service while
   the event is pending to prove retry, then after acknowledgment to prove no
   duplicate wake. A full Gateway restart remains separately authorized.

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

## 0.1.4: owner-session binding

In `owner` mode the wake service binds the owner SESSION, not a single run. Each
cycle it asks the hub for `GET /harness/factory-observer/continuity?owner_id=<ownerSessionKey>`
(runs whose `authorized_owner_id` equals the session key and that hold pending or
unacknowledged work), leases and wakes per run, and sends the wake with
`source: "hook"` so OpenClaw's heartbeat policy treats it as a wake payload
(`source: "other"` is dropped as `empty-heartbeat-file`). `runId` is optional and
kept for backward compatibility; tool calls may carry `run_id`, which must equal a
fixed `runId` when one is configured. The hub sets `authorized_owner_id` at launch
from the goal's `continuity_owner_id`, so no database rebind is needed.
