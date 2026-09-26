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
a launch target without a drive implementation; it is now correctly
observe-only, with a regression test preventing that specific failure.

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
for compatibility. A versioned nested capability object becomes the source of
truth for new clients:

```json
{
  "name": "codex",
  "enabled": true,
  "description": "Codex CLI",
  "capabilities": {
    "schema_version": 1,
    "launch_modes": ["structured"],
    "approvals": false,
    "interrupt": true,
    "native_resume": true,
    "model_catalog": true,
    "usage": true,
    "worktree": true,
    "attachments": true,
    "interactive_auth": true
  }
}
```

Rules:

- The schema is additive within one version.
- Unknown fields are ignored by clients.
- Missing capability data invokes a bounded legacy compatibility path during
  rollout; it does not imply every capability.
- New servers emit the matrix for every offered harness.
- A harness with no launch mode is not a launch target.
- Observe-only sources are not represented as disabled launch adapters.

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
   parsing, metrics, and historical compatibility.

Each migration step must be independently releasable. A mixed-version fleet
must continue to list and launch the capabilities an older host can prove.

## Test gates

The implementation is complete when:

- every offered launch target is backed by a registered adapter;
- every declared capability has a contract test proving the operation works or
  is rejected deterministically;
- adding a fixture adapter requires registry configuration and tests, not edits
  to the manager, daemon routing, web UI, or iOS harness-name lists;
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
