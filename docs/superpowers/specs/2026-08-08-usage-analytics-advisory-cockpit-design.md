# Usage Analytics and Advisory Cockpit Design

**Date:** 2026-08-08
**Status:** Approved for implementation planning

## Summary

Drover will expand its fleet inbox into a local-first usage and configuration
cockpit. The Home screen will answer four questions at a glance:

1. What needs the operator's attention?
2. How much capacity remains in each configured model-provider subscription,
   and when does its current window reset?
3. Which projects, harnesses, hosts, and models account for recent activity?
4. Which operational or system-configuration gaps deserve remediation?

Provider connectors remain authoritative for subscription usage, remaining
capacity, plan identity, and reset times. Drover events and OTLP telemetry are
authoritative for observed project, harness, host, model, token, cost, cache,
latency, routing, and delegation activity. The product will display these
sources together but will not pretend that one can be reconciled into the
other.

Configuration insights are advisory-only. Drover can identify a problem,
explain its evidence and likely impact, and provide exact remediation steps.
It will not rewrite prompts, hooks, skills, provider settings, or host
configuration.

## Goals

- Show a provider subscription inventory with current usage or remaining
  capacity, reset countdowns, freshness, and connector health.
- Summarize observed activity across projects, harnesses, hosts, and models for
  a selected time window.
- Rank popular projects using observed tokens when coverage permits, with an
  explicit fallback to session/activity counts when it does not.
- Detect operational problems such as stale connectors, broken hooks, missing
  attribution, absent cost/token fields, routing anomalies, and inefficient
  cache use.
- Analyze system prompts, prompt patterns, hooks, and skills for configuration
  gaps through an explicitly enabled content-analysis backend.
- Present deterministic failures and model judgments through one finding
  contract without blurring their different confidence levels.
- Preserve Drover's local-first behavior and continue providing useful fleet
  and usage information when optional integrations are absent.

## Non-goals

- Automatically changing any host, harness, hook, prompt, skill, or provider
  configuration.
- Inferring provider subscription capacity or reset time from observed tokens.
- Replacing AgentWeave, Tempo, Langfuse, or another tracing or evaluation UI.
- Requiring AgentWeave for the cockpit to function.
- Storing complete system prompts, skill documents, hook bodies, or model-review
  bundles in the context store.
- Comparing individual operators or implementing multi-user access control.
- Treating unsupported provider quota surfaces as if they supplied precise
  usage. Unsupported or unavailable usage is displayed explicitly.

## Product Structure

### Home

Home remains the entry point to live fleet work but becomes an analytics-first
cockpit. Its information hierarchy is:

1. sessions requiring input or approval;
2. provider-capacity cards with subscription name, remaining or used amount,
   reset countdown, freshness, and connector status;
3. recent observed activity totals for the selected default window;
4. popular projects with the harnesses and hosts contributing activity;
5. open configuration insights, summarized by severity;
6. live and recently finished sessions.

Healthy or unavailable sections remain compact. The screen must not turn into
a wall of persistent warning banners. Existing session navigation and launch
behavior remain intact.

### Analytics

The Analytics destination provides time-scoped drilldowns with filters for
host, harness, provider, model, and project. It contains:

- a provider-reported subscription and limit list;
- observed token, API cost, cache, latency, and session totals;
- usage distributions by project, harness, host, and model;
- project rows that show every contributing harness and host;
- source, freshness, and coverage labels on every aggregate that needs them.

Provider-reported limits and Drover-observed activity are visually distinct.
The UI may place them next to each other, but it must not calculate a false
allocation of subscription capacity to projects.

### Insights

The Insights destination is a prioritized feed of active findings. Filters
cover lifecycle state, severity, confidence, analyzer class, host, harness, and
target type. A finding detail view contains:

- title, severity, confidence, and analyzer class;
- affected host, harness, project, connector, or configuration target;
- why the issue matters and its expected impact;
- bounded evidence with timestamps and source references;
- exact guided-remediation steps;
- Check Again, Acknowledge, and Dismiss actions.

Check Again reruns the relevant analyzer. It never applies the remediation.
Host and harness detail views link into the same feed with contextual filters.

## Data Ownership and Source Boundaries

### Provider connectors

Provider connectors own plan identity, subscription capacity, usage windows,
reset times, and provider-side freshness. A connector may report one or more
concurrent windows. Drover preserves each window instead of collapsing them
into an invented total.

Harness hosts contribute an inventory of configured or detected providers.
When a provider does not expose a supported quota surface, Drover shows the
subscription or provider as detected with `usage unavailable`; it does not
scrape or estimate a quota from telemetry.

### Drover activity

Native harness events provide live session state and local activity even when
no observability source exists. OTLP spans enrich this with tokens, API cost,
cache behavior, latency, routing, delegation, and tool outcomes when producers
emit those fields.

AgentWeave is one supported OTLP producer. It is not a required Drover
subsystem or a special storage contract. If AgentWeave is disconnected, only
the metrics that depend on its spans lose coverage.

### Popular projects

For a selected window, popular projects are ranked by observed total tokens
when token coverage satisfies the response's completeness rule. If coverage is
insufficient, ranking falls back to normalized session/activity counts and the
UI names that metric. Repository attribution follows the context store's
existing identity and provenance rules; a weak project label does not invent a
repository identity.

## Architecture

The feature uses a unified advisory engine with typed analyzers:

1. Provider connectors and harness inventory produce subscription snapshots
   and health state.
2. Existing agent-event and OTLP pipelines provide normalized activity facts.
3. Change detection creates configuration target descriptors with stable target
   identity and content hashes.
4. Deterministic analyzers inspect operational facts and locally readable
   configuration metadata.
5. An explicitly enabled model analyzer reads an ephemeral, redacted content
   bundle for prompt, system-instruction, hook, and skill analysis.
6. Every analyzer emits a `FindingCandidate` through the same typed contract.
7. The advisory service deduplicates candidates, records bounded evidence, and
   advances finding lifecycle state.
8. Composed Home, Analytics, and Insights APIs serve the iOS presentation
   layer.

Deterministic and model analyzers remain separate modules behind the shared
interface. A deterministic analyzer can be tested and run without a model
backend. The model analyzer cannot represent its output as a confirmed fact.

## Storage Model

### Durable provider facts

`provider_usage_snapshots` is an append-oriented Parquet dataset exposed
through a DuckDB view. Each record contains:

- stable snapshot and deduplication identity;
- provider and account identifier or operator-supplied label;
- plan or subscription label;
- provider window identifier and window kind;
- used, remaining, limit, and unit fields as reported;
- window start and reset timestamps;
- observation time, connector version, status, and source provenance;
- raw-object reference when a retained provider response is permitted.

Secrets and bearer credentials are never written into snapshot facts or raw
objects.

### Operational connector state

`provider_connections` is mutable DuckDB state containing connector identity,
enabled state, last attempt, last success, last error classification, supported
capabilities, and credential reference. Credentials remain in the platform's
secret store or protected configuration and are referenced, not copied.

### Advisory state

`advisory_findings` stores the current derived projection:

- finding identifier and stable fingerprint;
- analyzer, rule, target type, and target identifier;
- analyzer class (`deterministic` or `model`);
- severity, confidence, title, impact, and remediation;
- lifecycle state and dismissal reason;
- first seen, last seen, resolved, dismissed, and regressed timestamps;
- evaluated content hash and latest analysis-run identifier.

The stable fingerprint is based on analyzer, rule, and target identity. Content
hash is not part of the fingerprint, so a finding retains its history across
target revisions.

`advisory_occurrences` stores append-only, bounded evidence for each observed
candidate: finding identifier, analysis run, timestamps, source references,
structured evidence fields, and short redacted excerpts. It never stores the
complete analyzed file or prompt bundle.

Existing pipeline jobs, attempts, and artifacts record analysis intent,
execution, failures, and produced result versions. The advisory engine does not
introduce an independent scheduler.

## Analyzer Contract

Each analyzer accepts an immutable analysis snapshot and returns zero or more
typed candidates. A candidate includes:

- rule and target identity;
- title and concise description;
- severity and confidence;
- expected impact;
- structured evidence and source references;
- exact remediation steps;
- analyzer class and evaluated content hash;
- optional coverage or uncertainty notes.

Deterministic analyzers initially cover connector freshness, hook validity,
telemetry flow, attribution gaps, missing token/cost fields, routing anomalies,
cache inefficiency, and internally inconsistent reset windows. Model analysis
covers unnecessary prompt repetition, conflicting system instructions,
inefficient prompt patterns, broken or ambiguous skill guidance, and semantic
hook/configuration gaps that cannot be established by syntax alone.

Severity describes impact if ignored. Confidence describes evidence strength.
Only deterministic evidence can use `confirmed`; model findings are limited to
`likely` or `speculative` and must state their uncertainty.

## Triggers and Scheduling

- Lightweight operational analyzers run as source facts and connector health
  change.
- Configuration analyzers run when a target's content hash changes.
- A scheduled full review catches environmental drift that does not alter a
  tracked file.
- Check Again enqueues a scoped analysis for one finding or target.
- A manual full refresh is available for operator diagnostics.

Jobs coalesce by analyzer, target, and source version. Repeated unchanged input
does not repeatedly call the model backend.

## Finding Lifecycle

New evidence creates or updates an `open` finding. Repeated candidates update
last-seen time and evidence without creating duplicate cards.

The operator may:

- acknowledge an open finding while leaving it active;
- dismiss it with a required reason;
- request a fresh check.

A finding becomes `resolved` only when its analyzer observes passing evidence,
not when the operator presses a completion button. If the same rule and target
fail again after resolution, the finding becomes `regressed` while preserving
its prior resolution history.

A dismissed finding reopens only when severity increases, target content
changes, or materially new evidence changes the recommendation. Ordinary
periodic re-observation does not defeat dismissal.

## Privacy and Consent

Deterministic checks run locally. Content-sensitive model analysis is disabled
until the operator explicitly enables it and selects an analysis backend.
Local backends are the default. External providers require a separate, clear
disclosure that prompt, instruction, hook, or skill content may leave the
device.

Content collection applies:

- explicit target allowlists;
- canonical path and symlink resolution;
- per-file and per-bundle size limits;
- credential and secret redaction before model submission;
- logging that records hashes and references, not submitted content;
- ephemeral bundle lifetime limited to one analysis attempt.

Revoking consent stops future model analysis. Existing derived findings remain
available until the operator deletes them. The operator can immediately purge
all retained redacted excerpts without deleting lifecycle metadata.

## API Shape

The server exposes authenticated endpoints under the existing private Drover
boundary:

- `GET /cockpit/overview` returns attention, latest provider capacity,
  observed activity summary, popular projects, insight counts, coverage, and
  freshness for a bounded time window.
- `GET /analytics` returns filterable, paginated provider and observed-activity
  breakdowns.
- `GET /insights` returns filterable, paginated finding summaries.
- `GET /insights/{finding_id}` returns detail and bounded evidence.
- `POST /insights/{finding_id}/acknowledge` records acknowledgement.
- `POST /insights/{finding_id}/dismiss` requires a dismissal reason.
- `POST /insights/{finding_id}/check` enqueues scoped reanalysis.
- `DELETE /insights/content-excerpts` purges retained excerpts without deleting
  finding lifecycle metadata.

Responses include source class, observation time, freshness, and coverage where
needed. Provider reset timestamps are absolute, timezone-aware values; clients
derive countdown text using their current clock.

## Failure and Degraded Behavior

Provider connectors fail independently. A failed connector leaves its last
successful snapshot visible with a stale label and last-updated time. It does
not blank other subscriptions or observed analytics.

Expired, negative, or contradictory reset windows become stale and trigger a
refresh. They are not displayed as negative countdowns. Unsupported quota
surfaces show `usage unavailable` rather than an estimate.

Missing OTLP or AgentWeave data lowers enrichment and coverage. It does not
disable fleet state, provider capacity, local activity, or deterministic checks
that have sufficient inputs.

Analyzer failures are isolated and retried through the pipeline ledger. Model
backend failure cannot block deterministic analyzers. API responses return
partial results with per-section status instead of failing the whole Home
screen.

## Verification Strategy

### Unit tests

- provider snapshot normalization and window handling;
- project ranking and token-coverage fallback;
- analyzer candidate contracts and confidence restrictions;
- stable fingerprinting and occurrence deduplication;
- acknowledgement, dismissal, resolution, reopening, and regression rules;
- redaction, allowlists, path resolution, and bundle size limits;
- change-aware job coalescing and scheduled-review behavior;
- stale and contradictory reset-window handling.

### Contract and API tests

- recorded, secret-free connector fixtures for each supported provider shape;
- Home composition with zero, partial, stale, and complete data sources;
- Analytics filtering, pagination, provenance, freshness, and coverage;
- Insights filtering, detail, lifecycle actions, and scoped Check Again;
- compatibility with absent AgentWeave and mixed OTLP producers;
- authentication on every new endpoint.

### iOS tests

- provider-capacity and reset-countdown presentation;
- stale, unsupported, and partial-data states;
- popular-project metric labeling and harness/host attribution;
- fleet, Analytics, and Insights navigation;
- finding severity, confidence, source-class, and lifecycle presentation;
- accessibility labels, Dynamic Type, and compact-screen layout.

### End-to-end acceptance

- Drover remains useful with no provider or observability connector enabled.
- A connector failure affects only its own section and preserves the last good
  snapshot with visible staleness.
- Provider capacity is never inferred from Drover-observed tokens.
- Analytics always disclose their source and coverage.
- Full system prompts, hooks, and skills do not appear in persisted findings,
  logs, or pipeline artifacts.
- No advisory action mutates a prompt, hook, skill, or host configuration.
- A repaired deterministic issue resolves only after new passing evidence, and
  a later recurrence is shown as a regression.

## Rollout Boundary

The implementation plan should stage the feature behind server capability
discovery so older iOS clients and servers continue to interoperate. The first
usable slice must support partial data: provider cards for implemented
connectors, local activity analytics, and deterministic advisories. Model-based
content analysis remains separately opt-in and can ship after the deterministic
path without changing the finding or UI contracts.
