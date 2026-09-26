# ADR 0001: Use a harness adapter registry and capability-driven clients

- **Status:** Accepted
- **Date:** 2026-09-25
- **Decision owners:** Drover maintainers
- **Architecture:** [Harness Adapter Architecture](../harness-adapter-architecture.md)

## Context

Drover drives several CLI harnesses through working provider-specific drivers.
Their construction, commands, worktree behavior, authentication, model
catalogs, and client behavior are selected through separate maps and
harness-name checks.

This makes a harness integration a cross-layer change. It also lets a client
infer unsupported operations from a name instead of receiving an executable
contract from the host. The failure mode is a control that appears valid but
fails only after the user invokes it.

Drover also ingests OpenClaw and Hermes activity. Ingestion is useful without
control, so the design must not equate an observed source with a launch target.

## Decision

Drover will introduce one registered `HarnessAdapter` contract per
drive-capable harness and expose a versioned capability matrix from each host.

The registry becomes the source of truth for driver construction, commands,
worktree policy, auth, model discovery, and supported session operations. Web
and iOS clients render controls from the advertised matrix rather than
hardcoded harness-name sets.

Provider wire formats remain inside adapters and continue to normalize into the
existing `StructuredMessage` vocabulary. Observe-only collectors continue to
implement the independent `Source` contract and do not enter the launch
registry.

The rollout preserves existing wire fields and supplies a bounded compatibility
path for older hosts and clients. Capability claims are validated and fail
closed.

Portable Profiles and automatic meta-harness scheduling are deferred. They may
consume this contract later but are not justification for expanding it before
the current drivers and clients migrate successfully.

## Consequences

### Positive

- One source of truth replaces duplicated maps and client assumptions.
- Unsupported controls are hidden or rejected before execution.
- Adding a harness has a testable, provider-neutral extension point.
- Mixed harness capabilities become explicit across web and iOS.
- Future delegation can select adapters without embedding provider protocols.
- Observe-only integrations remain honest and useful.

### Costs

- Existing drivers need compatibility adapters and contract tests.
- The host capability payload gains a versioned public schema.
- Mixed-version compatibility must be maintained during rollout.
- Capability granularity requires discipline; fields must represent executable
  behavior rather than speculative provider features.

### Risks

- A registry that merely wraps old maps without deleting duplication would add
  indirection without creating a source of truth.
- Overly broad capabilities could become a premature universal agent protocol.
- Removing legacy client behavior too early could break mixed-version fleets.

The acceptance gates in the architecture document address these risks.

## Alternatives considered

### Keep per-harness conditionals

Rejected. It preserves working code short-term but multiplies changes across the
daemon, web, and iOS for every harness and cannot guarantee capability honesty.

### Standardize one provider wire protocol

Rejected. Claude Code, Codex, agy, and DeepSeek have different lifecycle,
approval, resume, and transport semantics. Adapters should normalize Drover
operations and events without pretending the native protocols are identical.

### Let clients infer features from harness IDs

Rejected. It duplicates policy, drifts between clients, and cannot describe a
host running an older or differently configured adapter.

### Put observe-only sources in the registry as disabled adapters

Rejected. Collection and control are independent trust boundaries. A disabled
launch row would still imply a drive integration that does not exist.

### Build Profiles or a meta-harness first

Rejected. Both would depend on an unstable set of harness-specific branches.
The adapter registry must be proven by current migrations and one new adapter
before higher-level routing is justified.
