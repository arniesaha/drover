# Harness Adapter Architecture

## Status

Approved direction. This document defines the target command-plane boundary for
adding and operating harnesses. It does not introduce portable Profiles or a
meta-harness scheduler.

See [ADR 0001](adr/0001-harness-adapter-capability-registry.md) for the decision
and rejected alternatives.

## Problem

Drover can drive Claude Code, Codex, Antigravity (`agy`), and DeepSeek Harness,
but the integration contract is spread across several places:

- structured driver factories in `structured/manager.py`;
- default command tables and worktree policy in `harness/daemon.py`;
- authentication and model-catalog adapter tables;
- hardcoded harness-name sets in the web and iOS clients.

Those tables describe related properties without one source of truth. Adding a
harness therefore requires coordinated special cases, and clients can offer a
control that a selected harness cannot execute. OpenClaw previously appeared as
a launch target without a drive implementation. It is now observe-only end to
end (#421): no preset, adapter, envelope row, launch path or native-resume
argument exists for it or for Hermes, and
`test_observe_only_sources_are_never_drive_targets` keeps it that way while
their collection, parsing, attribution, metrics and recall stay unchanged.

## Goals

1. Make one registered adapter the source of truth for a drive-capable harness.
2. Advertise a versioned capability matrix to clients.
3. Make web and iOS render launch and session controls from capabilities rather
   than harness names.
4. Preserve current behavior while the four existing structured drivers move
   behind the contract.
5. Keep observe-only collection independent from drive adapters.
6. Give future orchestration one stable, provider-neutral surface.

## Non-goals

- Portable agent Profiles, profile import/export, or profile hosting.
- Automatic target selection or a meta-harness scheduler.
- A common provider wire protocol.
- Hosted multi-user operation, RBAC, or public-internet exposure.
- Claiming OpenClaw or Hermes as drive targets before real adapters exist.
- Changing the single-operator trusted-LAN/tailnet security boundary.

## Boundary

A **drive adapter** owns command-plane behavior for one harness. It converts
Drover's provider-neutral session operations into that harness's native CLI or
connection protocol and normalizes native output into `StructuredMessage`.

A collect **Source** owns context-plane ingestion. OpenClaw, Hermes, and other
observe-only systems remain Sources and never appear in a launch picker merely
because Drover can ingest their history.

A system may eventually implement both boundaries, but registration is
explicit. Collection never implies control.

## Adapter contract

The implementation should introduce a typed `HarnessAdapter` Protocol or ABC.
Names are illustrative; the implementation may split immutable metadata from
runtime methods while preserving this boundary.

```python
class HarnessAdapter(Protocol):
    id: str
    display_name: str
    capabilities: HarnessCapabilities

    def default_command(self) -> list[str]: ...
    def build_command(self, request: LaunchRequest) -> list[str] | ConnectionSpec: ...
    def start(self, request: LaunchRequest, emit: EmitFn) -> Driver: ...
    def auth_adapter(self) -> HarnessAuthAdapter | None: ...
    def model_catalog_adapter(self) -> CatalogAdapter | None: ...
    def health(self) -> AdapterHealth: ...
```

Session operations stay provider-neutral:

- start, recover/resume, close;
- send an idempotent turn using `client_turn_id`;
- answer a permission request only when supported;
- interrupt only when supported;
- normalize events to the existing `StructuredMessage` vocabulary.

Provider-specific resume IDs, command flags, approval frames, and model discovery
remain inside the adapter.

## Capability model

Capabilities describe executable behavior, not marketing categories. The first
wire version should cover only behavior already needed by Drover clients:

| Capability | Meaning |
| --- | --- |
| `launch_modes` | Supported session modes, such as `structured` or `pty`. |
| `approvals` | The adapter can surface and answer permission requests. |
| `interrupt` | A running turn can be interrupted safely. |
| `native_resume` | A native provider session can be resumed. |
| `model_catalog` | Models and supported reasoning efforts can be discovered. |
| `usage` | Provider or session usage can be reported. |
| `worktree` | The adapter supports or requires Drover worktree isolation. |
| `attachments` | Structured turns accept declared attachment types. |
| `interactive_auth` | Drover can run an interactive sign-in flow. |

The adapter registry must validate capability declarations against the methods
an adapter actually implements. A capability is not emitted merely because a
provider could support it in theory.

## Public host capability envelope

Existing harness rows retain `name`, `enabled`, `description`, and `command`
for compatibility. `/capabilities` on harnessd and the `capabilities` object
inside central `/harness` and `/harness/hosts` host rows carry the same envelope,
regardless of direct or relay transport. The nested matrix is schema v1:

```json
{
  "host_id": "mac-mini",
  "display_name": "Mac Mini",
  "kind": "macos",
  "harnesses": [
    {
      "name": "codex",
      "enabled": true,
      "description": "Codex CLI",
      "command": [],
      "capabilities": {
        "schema_version": 1,
        "harness_id": "codex",
        "launch_modes": ["structured"],
        "approvals": false,
        "interrupt": true,
        "native_resume": true,
        "model_catalog": true,
        "usage": false,
        "worktree": true,
        "attachments": ["image/gif", "image/jpeg", "image/png", "image/webp"],
        "interactive_auth": true,
        "turn_preferences": true
      }
    }
  ]
}
```

`turn_preferences` (additive in v1, #420) means model and reasoning-effort
overrides reach later turns of a running session. It is projected from the
adapter's immutable `turn_preferences_mutable` attribute, the same flag turn
dispatch already enforces, and is only true when `model_catalog` is too. Claude
Code fixes them at process start, so it publishes `false`. Hosts that predate
the flag omit it, which reads as `false` under the missing-boolean rule below.

Rows also carry an additive `display_name` (#422): the adapter's
`display_name`, or `Shell` for the daemon's terminal. It is presentation
metadata of at most 256 characters, never identity. Clients fall back to
`name` when a host predates it.

`attachments` is an array of accepted MIME types, not a boolean. `usage` follows
the executable adapter contract; a separate provider usage probe does not imply
adapter session-usage support. Fields describe adapter support, not current auth
state. `enabled` remains the host's availability gate. Shell advertises only
`pty` through the daemon's generic terminal implementation; provider declarations
come from the existing validated adapter registry. Publication invokes no auth,
health, model-discovery, or command-construction hooks.

### Mixed-version rules

- New hosts emit v1 for every preset. A preset with no registered drive adapter
  has no launch modes and is disabled. Observe-only Sources do not become presets.
- Launch requires `enabled == true`, a supported schema version, and an explicitly
  advertised launch mode. An empty launch-mode list forces `enabled: false`, so
  old clients also hide that row.
- **Legacy fallback is metadata only.** An old host's missing envelope or missing
  per-harness matrix advertises no operations to a matrix-aware client. Do not
  synthesize a matrix, select a mode by harness name, or infer approvals, resume,
  attachments, auth, or any other capability. Clients may show host/harness status
  and an upgrade explanation. Existing sessions remain listable.
- Central preserves legacy `enabled` values and absent matrices for the existing
  clients during this compatibility window. Old string-only harness lists remain
  metadata; they contain neither an enabled flag nor a mode. Matrix-less legacy
  names may contain spaces (for example `codex beta`); they are not registry IDs. Existing clients
  that ignore unknown fields keep their current behavior; this slice does not
  migrate their control logic (#419/#420).
- Optional v1 booleans missing on input default to `false`; missing attachments
  default to `[]`. `schema_version` and `launch_modes` are required on a matrix.
  A present `null` or malformed matrix is invalid, never a legacy fallback.
- Unknown fields are ignored and removed before persistence/proxying. Unsupported
  positive integer schema versions are retained as version/identity metadata
  with empty launch modes and `enabled: false`; they never invoke legacy fallback.
- `command` is retained as an empty array on new hosts. Central also clears any
  legacy command array before persistence or publication: launch commands,
  environment assignments, prompts, credentials, and native auth payloads are
  host-local. The current web/iOS pickers do not need command contents.

### Validation and bounds

The shared wire validator runs before host registration persistence, and again
when loading previously stored declarations. It projects only public fields.

| Limit | Value |
| --- | --- |
| HTTP registration/heartbeat body | 128 KiB, rejected before reading |
| Host capability envelope | 64 KiB of JSON, including unknown fields |
| Harness rows per envelope | 32 |
| Nested matrix | 4 KiB of JSON, including unknown fields |
| Launch modes | At most two unique values: `structured`, `pty` |
| Attachment types | At most 16 unique MIME types, 127 characters each |
| Versioned harness ID | At most 64 characters; lowercase letters/digits with hyphen separators |
| Matrix-less legacy harness name | Nonempty text, at most 256 characters |
| Host ID / display name / kind | 256 / 256 / 64 characters |
| Description | 1,024 characters |
| Legacy command input | At most 64 strings, 4,096 characters each; discarded |

JSON size checks use Python's ASCII-escaped JSON encoding (including default
separator whitespace), stopping at the limit. Publication does not probe hosts,
perform analytical reads, or bypass the existing fleet render cache (#331/#224).

Duplicate JSON keys, harness IDs, modes, and MIME types are rejected. A supplied
nested `host_id` must match the registration host; a matrix's `harness_id`, when
supplied, must match its row's `name` (omission uses the row identity). Heartbeat
URL and body identities must agree. Booleans are strict JSON booleans, not strings
or integers. Invalid incoming declarations return HTTP 400 without changing the
last accepted registration. Corrupt or invalid pre-upgrade stored envelopes
publish `{}` so one host cannot break fleet listings. Error messages never echo
rejected values or secret-bearing payloads. These checks do not grant new trust:
registration still uses the existing authenticated host boundary.

## Registry

The registry is the only place that binds a harness ID to:

- immutable display metadata and capabilities;
- structured driver construction;
- default command construction;
- worktree policy;
- auth and model-catalog adapters;
- health/auth probes.

The existing structured drivers remain provider-specific. Migration should move
registration and policy first, not rewrite working wire parsers.

Startup validation fails closed for an invalid adapter declaration. A broken
adapter may be reported as unavailable, but must not prevent unrelated adapters
or observe-only collection from running.

## Client behavior

Web and iOS consume the same host capability envelope.

- Show a harness in New Session only when `enabled` is true and
  `launch_modes` is non-empty.
- Select structured or PTY mode from `launch_modes`, not from the harness name.
- Show Approve/Deny only when `approvals` is true.
- Show Interrupt only when `interrupt` is true.
- Offer native resume only when `native_resume` is true.
- Load model and reasoning controls only when `model_catalog` is true.
- Offer interactive sign-in only when `interactive_auth` is true.
- Explain worktree isolation only when `worktree` is true.
- Enable attachment controls only for supported attachment types.

These are deterministic rendering rules. No model decides which controls are
visible.

### Web console (#419)

The web console derives every control from
`src/drover/server/web/static/harness_capabilities.js`, which `ui.py` inlines into
`harness.html` and `harness_terminal.html`. The pages and the module contain no
harness names; a test fails if one appears in their scripts. Decisions for the
questions #418 left to clients:

- **Fail closed.** Only a row with a schema v1 matrix, `enabled: true` and a launch
  mode this client drives can be launched or chosen as a Continue target. Flags
  must be JSON `true`; unknown additive fields and unknown mode strings are
  ignored. Malformed known flags, modes or MIME types close the whole matrix.
  A null, malformed or identity-mismatched matrix, or any other
  `schema_version`, offers nothing. A harness the host does not list (for
  example observe-only OpenClaw) has no controls.
- **Preferred mode.** The web drives both modes. If a harness advertises both,
  it picks `structured`, because approvals, interrupt, attachments and the model
  catalog are structured adapter operations, while a PTY session only exposes
  raw terminal I/O. The one-click workspace start uses the first
  structured-capable target in the host's advertised order, falling back to a
  PTY-only target. The launch body always sends the chosen `mode`.
- **Session controls.** A structured session (`session.mode == "structured"`)
  gets a turn composer. Interrupt, Approve/Deny and attachments appear only if
  its harness still advertises `structured` and the matching capability on that
  host. The attachment picker accepts only the advertised MIME types. A PTY
  session, including one from before the matrix, keeps terminal attach, Ctrl-C,
  keys and Kill: these are part of the `pty` mode, not of the `interrupt`
  capability. Model and effort pickers appear only for a structured launch with
  `model_catalog`. They send only an effort the selected model lists. Native
  resume candidates are fetched only for a Continue target with
  `native_resume`. Worktree isolation is explained only when `worktree` is true.
- **No stale controls.** Launch, Continue, turn, approval and interrupt handlers
  re-resolve capabilities from the latest envelope when they run. Session
  polling uses the current host returned with the session; handoff refreshes
  the fleet before submission. They do not
  trust a hidden or previously rendered control. An approval is answered only if
  its `request_id` is still the newest unanswered `approval_prompt`.
- **Upgrade guidance.** A host whose rows have no matrix shows its harnesses as
  disabled pills, with "Upgrade Drover on this host to launch from the web. Its
  existing sessions stay listed." A newer schema version asks the user to
  upgrade the Drover hub. Neither case triggers a name-based fallback.
- **Legacy window.** The web needs no legacy compatibility code: legacy rows are
  rendered as metadata only. The upgrade explanation is removed once central
  stops publishing matrix-less rows. That is the end of the compatibility window
  in the rollout above, after #420 ships.
- **Not offered on the web.** The web has no interactive sign-in or usage
  surface. The module exposes `interactiveAuth` and `usage` for a future one, but
  nothing is rendered.

Behavior change: until now the web started provider CLIs (Claude Code, Codex,
agy) as raw PTY terminals, even though their adapters advertise only
`structured`. Now they start as structured sessions, driven from the session
page. `shell` remains a PTY terminal. The PTY Send button now ends input with
`\r` for every harness. The Codex-only `\n` special case is gone, so an
already-running Codex PTY session from before this change gets a terminal
Enter.

### iOS client decisions (#420)

The iOS app implements the rules above in DroverKit `HarnessCapabilities`,
`HarnessOffer` and `HarnessControls`. It answers #418's open client questions
this way:

- **Fail closed.** iOS offers a control only when the session's host advertises
  it in a supported matrix. Until the first snapshot names the session's host,
  chat controls are unresolved and withheld. That covers Allow/Deny, interrupt,
  mid-session preferences and attachments. A harness missing from the snapshot
  is treated the same way. The model also refuses unadvertised actions before
  any request: `launch`, `interrupt`, `approve`, turns, handoff, attachment
  admission and turn-preference overrides. Withdrawn attachment support leaves
  the draft intact; a pending delivery is held for review before any retry.
- **Missing or unsupported matrices.** A host with no envelope, or rows with no
  matrix (string rows and matrix-less objects), is legacy metadata. Its
  sessions stay listable and openable, and its enabled names stay in
  `HostSummary.harnesses`. It offers nothing to launch, and the launch sheet
  says: "This host runs an older Drover that doesn't advertise harness
  capabilities. Update Drover on the host to launch or control sessions from
  iOS." A newer `schema_version` reports its version and asks for an app
  update. A `null` or malformed matrix, including a non-boolean flag, an
  unknown attachment element or a `harness_id` mismatch, is invalid and is
  never treated as legacy. Unknown additive fields and unknown launch-mode
  strings are ignored.
- **Preferred mode.** When a harness advertises both, iOS launches
  `structured`. Only structured sessions carry a starting prompt, attachments,
  approvals and run preferences. iOS uses `pty` only when it is the sole
  advertised mode. The default harness is the host's first structured-capable
  launchable row, so there is no longer a "prefer `claude-code`" rule.
- **Handoff targets** are the host's launchable rows whose preferred mode is
  structured. A PTY-only target would have the handoff seed typed into a
  terminal and run as commands.
- **Native resume.** iOS has no native-resume control today. "Continue
  session" is Drover's server-built handoff, not native resume. The flag is
  decoded and exposed as `HarnessControls.supportsNativeResume` for a future
  control. Since #422 the operation behind it is the adapter's `resume`, so
  adding that control needs no server change.
- **Terminal Ctrl-C** writes 0x03 into the PTY. It is a terminal key, not the
  adapter `interrupt` operation, and stays available for PTY sessions.
- **Refreshes.** Launch-sheet controls are derived from the current snapshot
  on every read. A refresh that withdraws the selected harness's launch mode
  moves the selection to the host's next launchable harness. A refresh that
  changes `model_catalog` re-selects run preferences, so an unadvertised
  catalog is never fetched or sent. Chat re-resolves controls on each metadata
  load.
- **Retiring legacy compatibility.** The legacy path is already metadata-only
  on iOS, so retiring it means removing string-row decoding and the upgrade
  copy, not withdrawing any operation. That retirement belongs to rollout step
  6 below, in the first iOS release after the hub refuses matrix-less host
  registrations. Until then, iOS keeps listing legacy hosts and explaining
  them.
- **Display versus control.** The launch sheet labels harnesses with the
  envelope's `display_name` (#422), falling back to the raw ID.
  `HarnessPresentation` still maps known IDs to display names and icons for
  session rows, which do not carry the envelope, and unknown IDs fall back to
  the raw name. A few
  non-control lines still parse provider wire formats or legacy rows by name.
  Each is marked `// harness-name:`. `NoHarnessNameBranchingTests` fails on any
  other quoted harness ID in iOS app or DroverKit sources, and on the retired
  `structuredCapableHarnesses` and `interactiveAuthHarnesses` names.

The additive `turn_preferences` flag needs both an upgraded host and hub to
reach clients. A schema v1 hub predating this field drops it during projection;
clients read its absence as false, keeping launch preferences while withholding
mid-session overrides. Older clients ignore the new field. Matrix-less hosts
remain visible but cannot launch or send structured operations; existing PTY
terminal input remains the bounded legacy path.

### Host enforcement and the extensibility gate (#422)

harnessd enforces the same contract it publishes. A PTY launch requires `pty`
in the harness's published contract. An enabled preset is not
terminal-launchable on its own, and only the daemon's shell has a matrix row
without an adapter. Native resume is the adapter's `resume` operation for
structured launches, and its `build_command` input for PTY adapters. harnessd
keeps no per-harness resume flags. Interrupt, approvals, attachments (by
declared MIME type), model catalog and native resume are refused with
`"<id> does not support <operation>"` before any row, file or provider call
exists.

Central Continue routes only from the target host's v1 row. It prefers
`structured`, needs `native_resume` for a native resume, and refuses a
matrix-less target with an upgrade message. Restart recovery is the host
adapter's decision. A test-only adapter with a capability mix unlike any
built-in passes through registry, envelope, hub, web and iOS fixtures without
an ID-specific branch. See
[design/adapter-extension-points.md](design/adapter-extension-points.md) for
the measured extension points and the contract changes it required.

## Compatibility and rollout

1. Add capability types, adapter Protocol, registry validation, and contract
   tests without changing session behavior.
2. Register the four existing structured drivers through compatibility adapters.
3. Replace duplicate default-command, factory, worktree, auth, and catalog maps
   with registry reads.
4. Emit capability schema v1 while preserving legacy harness fields.
5. Migrate web, then iOS, to capability-driven behavior with fixtures for old
   and new servers.
6. Remove the legacy client fallback only after the supported upgrade window.
7. Remove remaining dead OpenClaw drive/resume glue while preserving collection,
   parsing, metrics, and historical compatibility. Done in #421.

Each migration step must be independently releasable. A mixed-version fleet
must continue to list and launch the capabilities an older host can prove.

## Test gates

The implementation is complete when:

- every offered launch target is backed by a registered adapter;
- every declared capability has a contract test proving the operation works or
  is rejected deterministically;
- adding a fixture adapter requires registry configuration and tests, not edits
  to the manager, daemon routing, web UI, or iOS harness-name lists
  (`tests/test_adapter_extensibility.py`, #422);
- all four existing structured harness lifecycle suites pass unchanged;
- web and iOS fixtures cover different capability combinations;
- old host capability payloads retain a bounded compatibility path;
- OpenClaw and Hermes remain observable but absent from launch targets;
- model/reasoning validation still rejects unsupported combinations before a
  process starts.

## Security and observability

The registry does not widen Drover's trust model. Commands and credentials stay
host-local, secrets never enter the capability envelope, and central routing
continues to use authenticated host connections.

Adapter identity, capability schema version, selected launch mode, and rejected
unsupported operations should be observable without logging prompts,
credentials, native auth payloads, or provider tokens.

## Future consumers

Portable Profiles may eventually reference preferred capabilities, and a
meta-harness may select a registered adapter by capability and quota. Neither
belongs in this rollout. The adapter contract must first prove that existing
harnesses can migrate without behavior regressions and that one new harness can
be added without cross-layer special cases.
