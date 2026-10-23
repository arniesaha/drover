# Studio Rust harness migration readiness plan (gated; no cutover)

**Status:** planning-only / not ready to authorize a cutover
**Decision owner:** Nix
**As-of source:** `3b3da505a4ebc45654d094b218fc836c462c8737` (2026-10-22)
**Scope:** Studio only, if and only if every gate below is accepted in order.

This document creates no authority to install, activate, restart, drain, export,
import, sign, notarize, publish, alter a service/configuration/pin/plist, contact
a gateway, wake a GPU host, or interact with a physical device. It is a readiness
and evidence plan, not an operator runbook authorization. NAS Rust work, GPU-PC
pilot work, TestFlight, deployment, and physical-device work are expressly out of
scope.

## Factual repository snapshot

The checked-out commit is one commit after the `v0.6.28` tag:

| Item | Repository fact | What it does **not** prove |
| --- | --- | --- |
| Release lineage | `v0.6.28` resolves to `6cab48b15db3725b1c4121ff12301050a0f75f41`; the current source is `v0.6.28-1-g3b3da505`. | That the current release has completed all release acceptance or that any Studio host received it. |
| Rust source version | `harnessd-rs/Cargo.toml` declares `drover-harnessd` version `0.7.0`. | Existence, identity, signature, notarization, or installation of a `0.7.0` artifact. |
| Migration implementation record | The Phase 2 audit records Python drain/export, Rust import/dry-run, idempotent session/event import, preserved event IDs/dedup keys, and active-turn fencing as implemented and contract-covered. | An executed Studio drain, a successful Studio import, or acceptance against the exact future cutover SHA. |
| CI/pilot artifact design | CI defines checksummed, OIDC-attested pilot artifacts; the macOS release workflow defines Developer-ID signing, notarization, `codesign`, and Gatekeeper assessment. | A completed run for the proposed Studio SHA, a signed/notarized asset, or a released checksum. |
| OpenClaw owner package | The package has schema/tests and a mock SDK harness; repository records include synthetic authenticated Drover HTTP/store checks. | Live OpenClaw plugin registration/activation, permitted tool discovery, Gateway transport, parent messaging, or parent resume. |

No binary, release checksum, signature receipt, notarization submission/acceptance
record, Studio host inventory, or Studio service state is asserted by this plan.
The signing/notarization workflow is evidence of a supported release mechanism,
not evidence that a particular artifact has passed it.

## Non-negotiable ordered gates

No later gate may be started, represented as passed, or used as compensating
evidence for an earlier one. Nix records each result as **accepted**, **rejected**,
or **blocked**; anything other than accepted stops the sequence.

### Gate 1: current release fully complete

The current release must be independently closed before either the
reliable-task-followthrough canary or any Studio Rust consideration. Required
evidence, bound to the released tag and immutable commit, is:

1. release owner’s completion record, required CI/release checks, and release
   acceptance results;
2. the published release asset inventory with byte sizes and SHA-256 values;
3. for each applicable macOS harness artifact: Developer ID identity/team,
   hardened-runtime `codesign --verify` result, notarization submission ID with
   `Accepted` status, and `spctl` result; and
4. an independent reviewer’s confirmation that the records refer to the same tag,
   commit, and assets.

The tag and workflow definitions are present in this checkout, but the four
items above are not present here as completed release evidence. **Gate 1 is
blocked, not inferred.**

### Gate 2: independently accepted reliable-task-followthrough

Only after Gate 1 is accepted, conduct a separately authorized, genuine
authenticated OpenClaw parent-resume canary and obtain independent acceptance.
This plan does not authorize the canary or an activation attempt.

The canary must use OpenClaw's supported plugin installation/registration and
policy/permission mechanisms for the actual Studio runtime version. It must not
rely on a mock SDK, synthetic in-process adapter, direct database write, an
undocumented Gateway bypass, or a manual stand-in for parent resume. Its
redacted evidence bundle must bind a single canary/run ID to:

1. the supported runtime version, plugin package digest, normal registration
   diagnostics, and the policy/permission decision that made the tool available;
2. successful authenticated request identity at the supported Drover boundary;
3. a real parent-originated task and real parent-resume delivery observed in the
   OpenClaw runtime, including correlation ID and parent/session identity;
4. durable exactly-once/duplicate-safe behavior through a deliberately repeated
   delivery or replay, followed by an explicit authorized owner acknowledgment;
   and
5. Nix’s independent acceptance plus a second reviewer’s verification of the
   raw, access-controlled records.

The present mock adapter and synthetic authenticated HTTP/store tests prove only
their documented package/protocol behavior. They **do not prove live OpenClaw
plugin activation or parent resume**. The repository ledger and package README
state that normal Gateway transport, runtime discovery, parent messaging, and a
live canary remain untested. **Gate 2 is blocked until this real canary is
authorized, performed, and independently accepted.**

### Gate 3: Studio Rust migration consideration

Only after Gates 1 and 2 are accepted may Nix authorize a Studio-specific
readiness review. That review may approve a separately scoped cutover proposal;
it does not itself authorize a cutover. It must bind every artifact, test report,
and migration rehearsal result to one immutable source SHA and one Studio target
identity.

## Studio readiness prerequisites for Gate 3

### Contract and parity evidence

For the proposed immutable SHA, retain exact CI URLs/IDs and machine-readable
reports for the full Python contract lane, the Rust `rust_phase2` lane on both
supported build targets, Rust migration/delivery coverage, and required release
checks. The porting ledger must be reviewed for every explicitly dropped contract:
a drop is not parity evidence. Any divergence in public routes, authentication,
session/turn state, terminal behavior, relay behavior, recovery, worktree
retention, or structured-adapter behavior is a blocker unless independently
accepted as a deliberately unsupported Studio capability.

The evidence must include a human parity review of the capabilities that Studio
will actually advertise. In particular, later-phase limitations (interactive
auth, provider probes/model catalog behavior, non-supported adapters, updater
and watchdog automation) must stay disabled or be separately accepted; none may
be silently inherited from Python.

### Quiescence and active-turn fence

Before any future drain, the authorized Studio operator must demonstrate a
bounded quiet window: new launches and turns are fenced; active terminals are
closed or their owners explicitly resolve them; existing structured turns reach
a terminal state; and the drain response reports both `draining: true` and
`quiescent: true`. A busy/409 response, expiry of the fence, unknown active
owner, or forced stop is a failed acceptance, not a reason to proceed.

Record the pre-drain inventory, active session/turn IDs, timestamped fence
responses, and authorized-owner acknowledgments without recording bearer tokens
or transcript content.

### Python-to-Rust JSONL handoff and replay/dedup validation

An authorized future rehearsal must use the supported authenticated Python drain
gate and its read-only stopped-registry exporter. It must retain the original
Python registry unchanged and prove this by pre/post digest plus read-only
export evidence. The handoff manifest must identify the source SHA, host ID,
exporter version, export digest, record counts, session IDs, original event IDs,
sequence values, canonical dedup keys, worktree/lifecycle metadata, and
quarantine/receipt metadata.

Rust import must first pass validation/dry-run, then commit atomically. The
acceptance report must compare source and destination counts and identities,
show that a repeated import creates no extra events or sessions and does not
overwrite newer state, and show the prescribed normalization: stale PIDs are
cleared and formerly live sessions are errored until explicit supported recovery.
No imported lifecycle operation may execute automatically. Replaying the same
JSONL after a Rust process restart must preserve the same dedup result.

### Artifact, signature, and rollback evidence

Before a Studio proposal can be approved, retain the proposed Rust binary,
SHA-256, source digest, CI provenance/attestation verification, architecture,
and release record together. For a macOS artifact, require the actual
Developer-ID signature details, hardened-runtime verification, notarization
`Accepted` submission, and Gatekeeper assessment from the released asset, not
merely the workflow YAML. The previous Python executable, unit/plist/service
definition, configuration, credentials reference, registry backup location, and
rollback artifact/digest must be identified privately and remain intact.

Rollback acceptance is a rehearsed, time-bounded return to that verified Python
artifact and prior state without data loss or concurrent dual-writer operation.
This document neither changes nor names any Studio service/configuration/pin/
plist and does not authorize a restart to obtain this evidence.

## Acceptance matrix for a future authorized rehearsal

Nix owns the evidence ledger and final disposition for every row. “Authorized
owner” means the accountable Studio session/work owner, not a test fixture or a
plugin mock.

| Case | Required observable result | Minimum retained evidence | Acceptance owner |
| --- | --- | --- | --- |
| Quiescence / active turn | New work is fenced; all active work is terminal or explicitly resolved; no forced stop. | Redacted inventory, fence response, timestamps, owner acknowledgments. | Nix + Studio operator |
| Restart | Rust restart reconciles stored state; no duplicate driver, orphaned terminal, or unexpected auto-resume. | Before/after inventories, restart log excerpt, contract result, state digest. | Nix + independent reviewer |
| JSONL replay | Same handoff can be replayed after restart without extra session/event rows. | Handoff digest, import reports, count/identity comparison, post-replay query/report. | Nix |
| Duplicate delivery | Repeated delivery is collapsed by canonical dedup identity while legitimate distinct records remain. | Correlation IDs, dedup-key comparison, durable queue/store report. | Nix + independent reviewer |
| Authorized-owner acknowledgment | A real authorized owner receives the canary/handoff outcome and explicitly acknowledges once; unauthorized access is refused. | Redacted authenticated trace, authorization decision, ack/correlation record, refusal evidence. | Nix + authorized owner |
| Rollback | Previous verified Python path returns cleanly; no concurrent writers and no lost/replayed duplicate data. | Rollback timing, prior artifact digest, post-rollback health/inventory, reconciliation report. | Nix + Studio operator |

Any missing result, mismatch, duplicate, unclear authority, or inability to
reconcile is a blocker requiring investigation; it is never waived by a green
unit test.

## Current blockers and unknowns

1. No independent evidence in this worktree establishes full completion of the
   current release.
2. No actual release asset/signature/notarization/provenance record is available
   here for a proposed Studio Rust artifact.
3. No genuine authenticated Studio OpenClaw registration, permitted activation,
   parent-originated task, parent resume, or authorized-owner ack evidence exists.
4. The supported OpenClaw permission/policy and plugin-activation path for the
   actual Studio runtime is unknown. It must be confirmed by the integration
   owner before any future activation attempt; this plan makes none.
5. Studio target identity, current Python service state, active-turn inventory,
   rollback artifact, exact config/registry location, and local deployment
   authority are intentionally unknown and must be discovered only in a
   separately authorized Studio review.
6. No independent acceptance reviewer or evidence retention location has been
   named in the checked-out artifacts.

## Supported report-only completion and watcher handoff

Until all gates are accepted, the only permitted follow-through is a
report-only watcher. Nix may request redacted, access-controlled reports from
the existing supported release/CI and OpenClaw diagnostics owners; it must not
invoke plugins, alter policy, create a canary, contact a gateway, or access a
Studio/NAS/GPU runtime. The watcher’s output is a gate ledger with: source/tag
and asset identities, links/immutable report IDs, each acceptance signature,
unknowns, and an explicit `blocked`/`accepted` status. It must report “no new
evidence” rather than infer success.

Handoff for independent review: provide this document, the immutable source
SHA, the raw redacted Gate 1 release bundle, the raw redacted Gate 2 canary
bundle (only after authorization), contract/parity results for the candidate
SHA, and Nix’s evidence ledger to a reviewer who did not execute the canary or
the migration rehearsal. The reviewer must confirm gate ordering, artifact
binding, authorization boundaries, and every matrix row before Nix can mark
Gate 3 ready for a separately authorized decision.

## Evidence sources inspected for this plan

- [Rust Phase 2 audit](../runbooks/harnessd-rs-phase2-audit.md): implementation
  claims, known later-phase limits, migration and CI status terminology.
- [Rust GPU pilot runbook](../runbooks/harnessd-rs-gpu-pc-pilot.md): documented
  future drain/export/import/rollback evidence model; it remains GPU-only and
  is not executed or adopted as Studio authorization here.
- [macOS harness distribution](../harnessd-macos-distribution.md) and
  [notarization workflow](../../.github/workflows/harnessd-macos-notarize.yml):
  supported signing/notarization verification mechanism.
- [Contract porting ledger](../../tests/contract/PORTING.md) and
  [Rust migration contract](../../tests/contract/test_rust_migration_contract.py):
  parity accounting and replay/import coverage basis.
- [OpenClaw owner package README](../../plugins/openclaw-continuity-owner/README.md),
  [owner protocol](../openclaw-owner-protocol.md),
  [Factory observer bridge](../factory-observer-bridge.md), and
  [roadmap ledger](../../roadmap/ledger.md): explicit boundary between mock/
  synthetic evidence and untested live OpenClaw integration.
