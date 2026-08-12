# Analytics and Advisory Query Pressure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make observed-cost analytics and insight detail reliable by reducing repeated local DuckDB scans, honoring the daily advisory cadence, exposing analytics degradation in iOS, and containing expected socket disconnects.

**Architecture:** Materialize one request-scoped `filtered_sessions` temporary table for analytics consumers, and gate advisory material-version calculation by the scheduler's configured bucket. Keep server partial-failure contracts intact, add a DroverKit presentation value for the iOS unavailable state, and widen only the existing expected-disconnect boundary in the HTTP writer.

**Tech Stack:** Python 3.12, DuckDB, pytest, Swift 6, Swift Testing, SwiftUI.

## Global Constraints

- All analytics and advisory query work remains local; do not add external service calls.
- Do not increase DuckDB memory limits.
- Preserve analytics cursor, snapshot, pagination, and numeric result semantics.
- Preserve immediate manual insight rechecks.
- Do not restart or deploy the shared live services during implementation.
- Work only in the existing isolated harness worktree.

---

### Task 1: Gate automatic advisory source-version work by interval

**Files:**
- Modify: `tests/test_advisory_jobs.py`
- Modify: `src/drover/server/advisory/jobs.py`

**Interfaces:**
- Consumes: `AdvisoryScheduler.enqueue_due_full_review() -> list[Job]`
- Produces: the same interface, with `source_version_factory` invoked at most once per configured bucket after a successful pass.

- [ ] **Step 1: Write failing scheduler tests**

Add tests with a counting source-version factory proving: two calls in one bucket compute once; a new bucket computes again but unchanged versions enqueue nothing; an exception leaves the bucket retryable.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run pytest tests/test_advisory_jobs.py -k 'scheduler_uses_material_fact_versions or source_version' -q`

Expected: the same-bucket call count exceeds the literal expected count because current code invokes the factory on every poll.

- [ ] **Step 3: Implement the minimal interval guard**

In `enqueue_due_full_review`, return before version calculation when `bucket == self._last_bucket`; set `_last_bucket` only after the full version/enqueue pass succeeds. Retain `_last_source_versions` across buckets so unchanged material does not enqueue duplicate jobs.

- [ ] **Step 4: Run focused advisory tests and verify GREEN**

Run: `uv run pytest tests/test_advisory_jobs.py -k 'periodic_scheduler or source_version or material_fact_versions' -q`

Expected: all selected tests pass.

- [ ] **Step 5: Commit the task**

Run: `git add tests/test_advisory_jobs.py src/drover/server/advisory/jobs.py && git commit -m "fix(advisory): honor the automatic review interval"`

### Task 2: Materialize analytics session facts once per request

**Files:**
- Modify: `tests/test_cockpit_analytics.py`
- Modify: `src/drover/server/cockpit/analytics.py`

**Interfaces:**
- Consumes: `_session_facts_sql(filters, snapshot_at) -> tuple[str, list[Any]]`
- Produces: a private request-scoped materialization helper and analytics consumers that query one temporary `filtered_sessions` relation.

- [ ] **Step 1: Write failing materialization tests**

Add an integration test using the real DuckDB fixture and profiling/query observation to prove `spans_enriched` is scanned once for one `activity_analytics` response, plus a failure-path test proving no request temporary table remains available after the call unwinds.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run pytest tests/test_cockpit_analytics.py -k 'materializes_session_facts_once or removes_materialized_session_facts' -q`

Expected: scan count is greater than one or the temporary relation persists because current code embeds the base CTE in every statement.

- [ ] **Step 3: Implement request-scoped materialization**

Create a collision-free temporary table name per call, execute `CREATE TEMP TABLE ... AS <base SQL> SELECT * FROM filtered_sessions`, and change fingerprint/totals/breakdown helpers to accept a quoted relation name with no repeated base parameters. Drop the table in `finally`, preserving the original exception.

- [ ] **Step 4: Run analytics contract tests and verify GREEN**

Run: `uv run pytest tests/test_cockpit_analytics.py -q`

Expected: all analytics tests pass, including cursor snapshot changes, filters, pagination, and exact totals.

- [ ] **Step 5: Run formatting for changed Python files**

Run: `uv run black --check src/drover/server/cockpit/analytics.py tests/test_cockpit_analytics.py`

Expected: both files are already formatted, or run Black on only these two files and repeat the check.

- [ ] **Step 6: Commit the task**

Run: `git add tests/test_cockpit_analytics.py src/drover/server/cockpit/analytics.py && git commit -m "perf(analytics): reuse normalized session facts"`

### Task 3: Present observed-cost unavailability in iOS

**Files:**
- Modify: `apps/drover/DroverKit/Tests/DroverKitTests/CockpitPresentationTests.swift`
- Modify: `apps/drover/DroverKit/Sources/DroverKit/CockpitPresentation.swift`
- Modify: `apps/drover/Drover/Screens/Cockpit/AnalyticsView.swift`

**Interfaces:**
- Produces: `ObservedUsageSectionPresentation`, initialized from a `SectionEnvelope<ActivitySummary>`, with optional warning text and a stable accessibility identifier.

- [ ] **Step 1: Write failing presentation tests**

Add Swift tests proving `.error`/nil and `.unknown`/nil produce warning text that explicitly names observed usage and API cost, while `.ok` with data produces no warning. Assert a stable identifier literal through the presentation type.

- [ ] **Step 2: Run focused Swift tests and verify RED**

Run: `swift test --package-path apps/drover/DroverKit --filter CockpitPresentationTests`

Expected: compilation fails because `ObservedUsageSectionPresentation` does not exist.

- [ ] **Step 3: Implement the presentation type**

Add the minimal public Sendable/Equatable value that derives `warningText`, `accessibilityLabel`, and identifier from the activity envelope without manufacturing numeric data.

- [ ] **Step 4: Render the warning in AnalyticsView**

Always render the Observed usage heading. When the presentation has warning text, show it in a `CockpitCard` with an alert icon and the stable accessibility identifier; otherwise render the existing metrics and breakdowns unchanged.

- [ ] **Step 5: Run focused and full DroverKit tests**

Run: `swift test --package-path apps/drover/DroverKit --filter CockpitPresentationTests`

Then: `swift test --package-path apps/drover/DroverKit`

Expected: all tests pass.

- [ ] **Step 6: Commit the task**

Run: `git add apps/drover/DroverKit/Tests/DroverKitTests/CockpitPresentationTests.swift apps/drover/DroverKit/Sources/DroverKit/CockpitPresentation.swift apps/drover/Drover/Screens/Cockpit/AnalyticsView.swift && git commit -m "fix(ios): explain unavailable observed cost"`

### Task 4: Contain disconnects during HTTP headers

**Files:**
- Modify: `tests/test_metrics.py`
- Modify: `src/drover/server/web/app.py`

**Interfaces:**
- Consumes: `_MetricsHandler._send(...) -> None`
- Produces: the same interface, returning normally on `BrokenPipeError` or `ConnectionResetError` from either headers or body writes.

- [ ] **Step 1: Reverse the existing header-disconnect expectation**

Change the existing test so an `end_headers` `BrokenPipeError` or `ConnectionResetError` must not escape. Keep the payload-disconnect coverage and assert no payload write is attempted after header failure.

- [ ] **Step 2: Run focused HTTP writer tests and verify RED**

Run: `uv run pytest tests/test_metrics.py -k 'disconnect' -q`

Expected: the header-disconnect test fails because `_send` still calls `end_headers()` outside its expected-disconnect handler.

- [ ] **Step 3: Implement the minimal disconnect boundary**

Wrap `end_headers()` and `wfile.write(payload)` in one expected-disconnect handler, log the existing concise message once, and return without swallowing any other exception type.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `uv run pytest tests/test_metrics.py -k 'disconnect or compression' -q`

Expected: all selected tests pass.

- [ ] **Step 5: Commit the task**

Run: `git add tests/test_metrics.py src/drover/server/web/app.py && git commit -m "fix(http): contain client disconnects during headers"`

### Task 5: Integrated verification

**Files:**
- Verify all files changed by Tasks 1-4.

**Interfaces:**
- Consumes: complete implementation and committed tests.
- Produces: fresh evidence for scheduler cadence, analytics correctness, Swift behavior, formatting, and repository cleanliness.

- [ ] **Step 1: Run focused Python suites together**

Run: `uv run pytest tests/test_advisory_jobs.py tests/test_cockpit_analytics.py tests/test_metrics.py -q`

Expected: zero failures.

- [ ] **Step 2: Run the full Python test suite**

Run: `uv run pytest -q`

Expected: zero failures, or report only independently reproduced pre-existing environment failures with evidence.

- [ ] **Step 3: Run the full DroverKit suite**

Run: `swift test --package-path apps/drover/DroverKit`

Expected: zero failures.

- [ ] **Step 4: Run formatting and diff checks**

Run: `uv run black --check src/drover/server/advisory/jobs.py src/drover/server/cockpit/analytics.py src/drover/server/web/app.py tests/test_advisory_jobs.py tests/test_cockpit_analytics.py tests/test_metrics.py`

Run: `git diff --check HEAD~4..HEAD`

Expected: both exit successfully.

- [ ] **Step 5: Inspect branch state without deploying**

Run: `git status --short && git log --oneline --decorate -6`

Expected: clean worktree with the design, plan, and task commits on the isolated branch. Do not restart live services.
