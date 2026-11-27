# Outcome delivery

Status: documentation baseline reviewed against `7133442d` on 2026-11-26.
This page distinguishes implemented boundaries from product direction. It is
not a new acceptance API, a release certificate, or evidence that pending work
has shipped.

## Product direction

Drover should be a durable execution and orchestration layer on machines the
operator controls, with replaceable harnesses and clients. The unit of success
is an independently accepted result. Sessions, issues and token counts help
operate the system; none is itself proof that the intended result exists.
Self-hosting is supported today on a trusted private network. Cloud placement
under the same ownership model is a deployment direction, not a managed cloud
service or verified cloud installation claim.

## What exists today

- Per-host harness daemons own local processes and filesystem authority;
  the hub routes control and retains durable context and work state.
- Registered adapters advertise versioned capabilities. Observation/collection
  and execution are separate: OpenClaw and Hermes ingestion does not establish
  drive support. Grokbot is not a verified orchestration integration.
- PostgreSQL-backed operational state and the durable outbox support replayable
  context ingestion; DuckLake serves verified analytical facts. This durability
  does not establish exactly-once execution of arbitrary external actions.
- Configured `local_file` native-owner canaries have automatic isolated work,
  fresh-worktree verification and owner acknowledgement boundaries.
- Real GitHub-backed workers execute in isolated worktrees and commit results;
  the integration owner separately reviews, publishes and accepts the work.
  Do not generalize the canary proof to all repositories or harnesses.

The detailed authority remains [verified event-path boundaries](operations/verified-event-path-boundaries.md),
[adapter capabilities](harness-adapter-architecture.md), and the
[work queue design](design/work-queue.md). An accepted architecture boundary
is not evidence that every environment currently meets it.

## Owner acceptance checklist

Use this as an operating checklist, not a claim that all checks are automated:

1. State the intended observable result, scope, non-goals and owner-approved
   criteria before execution. Distinguish a behavioral requirement from issue
   metadata and bookkeeping.
2. Check the selected host/harness can perform the actual required work:
   credentials, worktree writes, required checks and result delivery. An
   advertised launch capability does not prove effective tool permissions.
   A successful process launch alone is not a permission preflight.
3. Bind review and CI to the exact candidate revision. Retrieve authoritative
   check results and behavioral artifacts; reject missing, stale, wrong-source
   or skipped evidence rather than trusting a worker's success assertion.
4. Independently inspect the changed behavior. Passing fixtures or screenshots
   establish only the behavior they actually exercise. A merged PR, uploaded
   binary or finished session alone does not establish user acceptance.
5. Before retrying after interruption, inspect durable state and actual effects.
   Preserve the original scope and accepted results; reconcile uncertain effects
   before repeating them. Do not infer blanket exactly-once side effects.
6. Report attempted and accepted outcomes separately, with revision, check and
   behavioral evidence, unresolved limits and the next owner action.

## What still needs to be demonstrated

The quality cohort [#1024](https://github.com/arniesaha/drover/issues/1024)
targets trusted GitHub outcome acceptance, structural guardrails and three
bounded task-class pilots: bug fix, behavior-preserving refactor and feature.
That scope is work in progress at this documentation baseline, not a completion
claim. Even three accepted pilots would not prove general reliability.

The broader product requires retained cross-harness outcome evidence, effective
permission preflights, interruption/retry evidence without duplicate effects,
negative controls that reject known-bad evidence, and a fresh operator following
reproducible documentation. Harness-agnostic means preserving these contracts
when a harness or client changes, not pretending all harnesses have equal tools.

## Deployment and release boundaries

Use the [normal private server update](operations/private-server-update.md)
runbook for compatible private installs. Verify the exact artifact and changed
behavior, retain rollback, and use coherent backups for real state migrations.
Public release, App Store acceptance and physical-device acceptance are separate
from private installation or simulator screenshots. Public-internet exposure and
multi-user isolation remain unsupported; see [Security](security.md).
