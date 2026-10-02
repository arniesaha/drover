# Memory integrity and DuckLake recovery baseline

> **Status:** reconstructed approved-decision baseline (2026-10-01)
>
> The originally referenced planning artifact was not present in the repository
> history or the available local/remote checkouts when this was reconstructed.
> This is **not** represented as a recovered copy of that artifact. It records
> only the approval constraints supplied for the recovery, so later review has
> an explicit, auditable baseline.

## Approved architecture

1. PostgreSQL is the control store for durable operational state and jobs.
2. There is one canonical session/event stream. Raw events and original
   transcripts are deduplicated and preserved; source-native identities remain
   available for provenance.
3. Each session has one versioned summary projection. Summaries, briefs,
   embeddings, and queued derived jobs are replaceable outputs, not raw truth.
4. The PostgreSQL job/state path is the single source for derived-memory work;
   pgvector is used for semantic retrieval where available.
5. DuckLake/analytics processing remains process-isolated from the control
   plane. It is not a second command or memory queue.
6. Pond is removed. Spans are optional archival/diagnostic data, not a core
   dependency for session memory or recall.

## Preservation and cutover boundaries

- Preserve the PostgreSQL control store and the deduplicated raw events and
  original transcripts.
- Do **not** perform Phase 4 cutover, regeneration, deletion, or other
  destructive data edits as part of recovery work.
- Derived summaries, briefs, and jobs may be discarded and regenerated only in
  a separately approved Phase 4 operation.
- Take and verify a backup before any state-changing cutover work.

## Acceptance gates before a production cutover

- 25-session audit
- memory/retrieval audit completes in under five minutes
- search freshness validation
- 24-hour soak
- restore verification

## Phase mapping used for recovery review

- **M2 integration:** capability-driven web/iOS harness controls and bounded
  analytical-cache residency diagnostics. This is additive recovery work and
  does not alter the data cutover boundary.
- **Phase 3:** PostgreSQL-backed derived-memory ledger, versioned summary
  projection, jobs/leases, and pgvector integration. It preserves raw data and
  leaves derived-memory regeneration explicit.
- **Phase 4:** DuckLake cutover and any authorized derived-data regeneration;
  intentionally out of scope for the recovery PRs.

## Review note

This baseline was reconstructed from explicit approved decisions, not inferred
from code. If the original planning artifact later appears, reconcile it against
this document before authorizing Phase 4.
