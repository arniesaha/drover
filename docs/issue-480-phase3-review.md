# Issue #480 Phase 3 recovery review

Reviewed independently on Arnabs-Mac-Studio.local. Scope: recover the stopped
implementation onto current main, review correctness, and test locally without
runtime changes. Phase 4 / DuckLake remains out of scope.

## Recovery and scope

- Base: `origin/main` **97849f2**, fetched again after testing; unchanged.
- Isolated branch: `drover/harness-be85dbf5-115c-425d-aea4-3ba050510364`.
- Code commits: **4d58db4** (recovered a8243ac), **4236c5b** (recovered and
  corrected ece1463).
- Original worktree remains clean at **ece1463** (verified with optional index
  writes disabled). No duplicate writer or agent was launched.
- Read [issue #480](https://github.com/arniesaha/drover/issues/480) and the
  program acceptance criteria in #476. **The approved plan was not available**:
  `roadmap/plans/2026-10-01-memory-integrity-ducklake.md` was absent from the
  remote roadmap main and local checkouts searched. Its location was requested;
  plan-conformance review remains outstanding.
- Main's canonical identity/substantive transcript work (#485), host retirement
  (#486), Pond removal (#484), and optional span/session graph changes (#483)
  are retained. PostgreSQL migrations 5/6 remain intact; memory uses 7 and
  conditional pgvector migration 8.
- No push, PR, merge, deploy, runtime/config modification, production restart,
  or production data/lake mutation was performed. Tests used scratch files and
  disposable PostgreSQL schemas/clusters; fixture HTTP servers are test-only.

## Findings and corrections

1. Removed automatic legacy recap table drops from migration bootstrap. New
   derived tables start empty; legacy data is preserved. Cleanup remains an
   explicit dry-run-first command and now requires a PostgreSQL memory store.
   An upgrade regression test preserves existing recap rows, parent-session
   metadata, and host lifecycle metadata.
2. Atomic `FOR UPDATE SKIP LOCKED` claims create per-claim tokens and attempt
   rows in one transaction. A locked due row does not block a healthy sibling.
   Every finish/heartbeat/release operation now rejects expired tokens even
   before reclamation. Expiry consumes the bounded retry budget on reclamation.
3. Per-subject transaction advisory locks serialize enqueues even when no live
   row exists, fixing the caller-transaction race that could silently lose the
   competing generation. Recap sequence comparison uses the same lock and
   remains forward-only. Idempotency, retry backoff, quarantine, supersession,
   failure disposition reasons and retry exhaustion have PostgreSQL coverage.
4. Summary publication and embed/brief enqueue commit together behind the lease
   fence. Substantive transcript selection, deterministic files/tools, and final
   commit/issue references are retained from main. Acceptance reads, identity
   projection/link repair, and Project Activity now use the PostgreSQL memory
   repository. Native SessionEnd hooks retain intent before collector ingestion.
5. Explicit canonical harness summaries hide native duplicate artifacts in
   recent/keyword/semantic recall. Harness/native summary and close lookups
   preserve canonical resolution and explicit unmapped results. Retired-host
   filtering and optional span diagnostics remain intact.
6. Vectors validate model, dimension, numeric/finite values, and nonzero length
   in cosine space. Malformed rows quarantine individually while siblings finish.
   Semantic reads and embed-only backfill ignore embeddings whose source version
   differs from the current summary. PostgreSQL token/transaction behavior is
   covered; actual vector persistence/search requires pgvector and was skipped.
7. `/readyz` reports memory availability, vector availability, embedding backend
   configuration, and per-kind job freshness/backlog. Missing pgvector fails
   memory readiness explicitly; keyword recall remains available. Quality/audit
   and Prometheus use the PostgreSQL ledger. Span embedding maintenance and
   Redis/DuckDB memory coordination are retired; the analytical advisory ledger
   remains separate.

## Validation

Environment: Mac Studio, CPython 3.14.7, Homebrew PostgreSQL **17.11**, no pgvector.
All pytest runs were foreground, without xdist or background shell jobs.

| Run | Exact result | Follow-up |
| --- | --- | --- |
| Full backend: `uv run pytest -q` | **3599 passed, 14 skipped, 2 failed, 9 warnings; 574.96 s** | Failures were a stale DuckDB summary test update and a missing fixture argument; both corrected and passed in focused runs. |
| Broader recovery focus: changed test files plus host retirement, MCP server, activity HTTP, documented CLI and runtime roles | **976 passed, 13 skipped, 1 failed, 5 warnings; 191.37 s** | Exposed a SessionEnd intent regression introduced during review; corrected and verified by final run. Nine skips were pgvector coverage; four were DSN-gated benchmark/runtime-role integration. |
| Final focused run below | **224 passed, 5 skipped, 0 failed, 5 warnings; 37.12 s** | All five skips are pgvector integration. |
| Formatting and diff checks | **Passed** | isort check, Black check over all 78 changed Python files; `git diff --check`. |

Final focused command:

```sh
uv run pytest -q -rs \
  tests/test_embeddings.py tests/test_memory_store.py tests/test_ledger.py \
  tests/test_hook_cli.py tests/test_session_close_tool.py \
  tests/test_memory_integrity.py tests/test_mcp_tools.py \
  tests/test_mcp_tools_brief_recall.py tests/test_postgres_control_store.py \
  tests/test_server_cli.py
```

The full suite was not rerun after the final corrections. Earlier passing
coverage is not presented as a fresh all-green full run. Warnings were MCP transport deprecations and Python 3.14
multithreaded-fork deprecation in the cursor concurrency test.

Passing embedding worker tests use actual PostgreSQL job transactions and an
in-memory vector persistence double. They do **not** prove pgvector storage or
cosine SQL. Final skips cover real worker persistence, exact cosine search,
stale-generation search, and two semantic MCP recall contracts. Plain local/CI
PostgreSQL cannot provide that coverage without pgvector.

## Readiness and remaining risks

**Not ready for PR sign-off yet:** approved-plan conformance remains unverified.
Actual pgvector integration must also be exercised on an appropriately equipped
disposable database before merge. No production regeneration, fleet acceptance
run, provider call, five-minute memory SLO, or production MCP latency measurement
was performed. A future authorized upgrade needs explicit regeneration; this
review neither cut over the runtime nor deleted legacy data.
