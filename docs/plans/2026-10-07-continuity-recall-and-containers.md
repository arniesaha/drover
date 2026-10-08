# Continuity recall and context containers

## Findings and scope

Issue #475 chooses one hub recall endpoint. Issue #476 requires recorded sessions
to become recallable and resumable. This change adds provenance and a producer;
it does not enable production features or run production backfills.

Current paths (line numbers refer to the baseline):

- `src/drover/server/mcp/freshness.py:28`: recursive envelope stamping hardcodes
  hub identity and omits several computable timestamp fields.
- `src/drover/server/mcp/contract.py:153`: direct bounded reads stamp results;
  transport admission also stamps empty/error/timeout responses.
- `src/drover/server/mcp/tools.py:298`: summary projections preserve generation
  timestamps; context readers at lines 1011, 1058, 1093 and 1144 read containers.
- `src/drover/schema.py:190`: existing container table has stable primary keys,
  confidence, evidence, activity, links and redaction policy. No migration needed.
- `src/drover/server/memory_store.py:353`: PostgreSQL final summaries;
  lines 448 and 460 expose project briefs.
- `src/drover/server/lake/coverage.py:124`: authoritative context publication;
  line 202 certifies it for selected DuckLake reads. Never bypass this fence.
- `src/drover/config.py:584`: derived worker configuration defaults.
- `src/drover/server/__main__.py:1864`: context CLI; line 3171 starts workers.
- `src/drover/server/advisory/redaction.py:67`: shared credential redactor.

## Design

Keep response changes additive. Retain per-record producer host and distinguish
hub envelopes from non-authoritative local stores. Preserve existing watermarks
across repeated stamping and use generation, activity and source timestamps when
available. Empty recall may use an explicitly labelled derived-store watermark;
retrieval time never stands in for data time. PostgreSQL registered central stores
are hub stores; other stores explicitly identify as local/non-authoritative.

Add a hub-only context worker, disabled by `[context_containers] enabled = false`.
Build deterministic session and project container keys from source identity.
Repository evidence yields code_project; otherwise use general_activity with
explicit conservative confidence. Do not invent personal/research classifications.
Copy only allowlisted derived fields, redact before persistence, and retain
existing policies. Restrictive or unknown policies prevent copying content.
Use source generation time for updates and ended/activity time for last activity.
Idempotent upserts retain creation time and do not advance timestamps on reruns.

Use the existing analytical container table in compatibility mode. For DuckLake,
produce and certify an authoritative context revision through coverage APIs so
all four readers keep their existing selection and integrity guarantees. Report
publication bounds explicitly rather than silently publishing partial snapshots.

Add `drover-server context backfill-containers`, dry-run unless `--apply` is given.
Dry-run reads sources and reports counts without writes or publication. Runtime
worker and CLI share the same builder/upsert path. No raw transcripts are copied.

## Commit sequence and verification

1. This plan only, checked with `git diff --check`.
2. Identity/freshness and tests: foreground pytest for MCP freshness, contract,
   recall bundle and brief recall tests.
3. Writer/config/runtime and tests: seed PostgreSQL summaries and briefs, build
   containers, exercise all four tools, rerun for idempotence, update sources,
   test restrictive policies and credential redaction. Test config defaults and
   lake publication integration. No migration anticipated.
4. Dry-run backfill CLI and tests: verify no changes by default, explicit apply,
   non-hub refusal and rerun counts.
5. Documentation: endpoint authority, watermark meanings, classification policy,
   flag and backfill usage/limits. Run scoped regression tests and diff checks.

Every implementation commit passes its scoped tests. Public changes contain no
private infrastructure addresses or paths and no em dashes. Commit only: no push,
PR, merge, deployment, host config change or production backfill.
