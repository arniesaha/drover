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
  program acceptance criteria in #476. The originally referenced plan was not
  available in the repository history or searched checkouts. A clearly labelled
  reconstructed approved-decision baseline is supplied with the companion M2
  recovery PR at `roadmap/plans/2026-10-01-memory-integrity-ducklake.md`; it is
  not represented as the lost original. This Phase 3 scope is compatible with
  that baseline: it preserves raw/control data, makes derived-memory work
  PostgreSQL-backed and versioned, leaves DuckLake process-isolated, and does
  not authorize Phase 4 regeneration or cutover.
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
   differs from the current summary. PostgreSQL token/transaction behavior and
   actual pgvector persistence/search are covered with a disposable pgvector
   PostgreSQL instance.
7. `/readyz` reports memory availability, vector availability, embedding backend
   configuration, and per-kind job freshness/backlog. Missing pgvector fails
   memory readiness explicitly; keyword recall remains available. Quality/audit
   and Prometheus use the PostgreSQL ledger. Span embedding maintenance and
   Redis/DuckDB memory coordination are retired; the analytical advisory ledger
   remains separate.

## Validation

Environment: Mac Studio, CPython 3.14.7, Homebrew PostgreSQL **17.11** with
pgvector installed. All pytest runs were foreground, without xdist or background
shell jobs. The disposable PostgreSQL/pgvector result below is recorded recovery
handoff evidence; it did not touch production state.

| Run | Exact result | Follow-up |
| --- | --- | --- |
| Full backend with disposable real pgvector | **3606 passed, 15 skipped** | Confirms the Phase 3 suite against real vector storage/search. |
| Targeted pgvector/inverse coverage | **49 passed, 3 expected inverse skips** | The inverse skips assert behavior only relevant when pgvector is unavailable. |
| Formatting and diff checks | **Passed** | isort check, Black check over all changed Python files; `git diff --check`. |

Final focused command:

```sh
uv run pytest -q -rs \
  tests/test_embeddings.py tests/test_memory_store.py tests/test_ledger.py \
  tests/test_hook_cli.py tests/test_session_close_tool.py \
  tests/test_memory_integrity.py tests/test_mcp_tools.py \
  tests/test_mcp_tools_brief_recall.py tests/test_postgres_control_store.py \
  tests/test_server_cli.py
```

The final full run was performed with a disposable real pgvector PostgreSQL
instance after pgvector was installed on the Studio. It validates real vector
persistence and cosine search without modifying any production store. The
remaining skips are expected environment/capability skips, not failed tests.

## Readiness and remaining risks

No production regeneration, fleet acceptance run, provider call, five-minute
memory SLO, or production MCP latency measurement was performed. A future
authorized upgrade needs explicit regeneration and the baseline acceptance gates
(25-session audit, under-five-minute audit, search freshness, 24-hour soak, and
restore verification). This review neither cut over the runtime nor deleted
legacy data.
