# Cockpit Advisory Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a deterministic, evidence-backed advisory engine with stable finding lifecycle, pipeline-ledger jobs, and authenticated Insights APIs.

**Architecture:** Typed analyzers consume immutable operational snapshots and emit one `FindingCandidate` contract. A repository deduplicates candidates by analyzer/rule/target, appends bounded occurrences, and advances explicit open/acknowledged/dismissed/resolved/regressed state. Existing pipeline tables remain the durable job and attempt ledger.

**Tech Stack:** Python 3.11, DuckDB, existing Drover pipeline ledger and job streams, pytest.

## Global Constraints

- Advisories never mutate prompts, hooks, skills, provider settings, or host configuration.
- Severity describes impact; confidence describes evidence strength.
- Only deterministic evidence may use `confirmed` confidence.
- Findings resolve only after passing analyzer evidence.
- Dismissal requires a reason and reopens only for higher severity, changed target hash, or materially changed evidence.
- Full configuration content is never stored in finding or occurrence tables.
- API failures are isolated from provider analytics and fleet state.

---

## File Structure

- `src/drover/server/advisory/types.py`: analyzer inputs, candidates, evidence, and lifecycle enums.
- `src/drover/server/advisory/repository.py`: finding persistence and state transitions.
- `src/drover/server/advisory/analyzers/`: deterministic analyzers by responsibility.
- `src/drover/server/advisory/jobs.py`: source-versioned enqueue/coalescing helpers.
- `src/drover/server/advisory/worker.py`: run analyzers and record ledger attempts/artifacts.
- `src/drover/server/advisory/service.py`: list/detail/action API service.
- `src/drover/schema.py`: finding and occurrence tables.
- `src/drover/server/web/app.py`: Insights GET/POST routing.
- `tests/test_advisory_repository.py`: lifecycle state machine.
- `tests/test_advisory_analyzers.py`: deterministic analyzer behavior.
- `tests/test_advisory_jobs.py`: coalescing, attempts, and failures.
- `tests/test_metrics.py`: authenticated HTTP integration.

### Task 1: Finding contract, storage, and lifecycle repository

**Files:**
- Create: `src/drover/server/advisory/__init__.py`
- Create: `src/drover/server/advisory/types.py`
- Create: `src/drover/server/advisory/repository.py`
- Modify: `src/drover/schema.py`
- Test: `tests/test_schema.py`
- Test: `tests/test_advisory_repository.py`

**Interfaces:**
- Produces: `FindingCandidate`, `FindingEvidence`, `AdvisoryRepository.observe()`, `.mark_passing()`, `.acknowledge()`, `.dismiss()`, `.list_findings()`, and `.get_finding()`.

- [ ] **Step 1: Write failing lifecycle tests**

```python
def test_observation_deduplicates_and_regresses(repository, candidate):
    first = repository.observe(candidate, run_id="run-1")
    repository.mark_passing(first.finding_id, run_id="run-2")
    again = repository.observe(candidate, run_id="run-3")
    assert again.finding_id == first.finding_id
    assert again.state == FindingState.REGRESSED

def test_dismissed_finding_only_reopens_for_material_change(repository, candidate):
    finding = repository.observe(candidate, run_id="run-1")
    repository.dismiss(finding.finding_id, reason="accepted tradeoff")
    assert repository.observe(candidate, run_id="run-2").state == FindingState.DISMISSED
    changed = replace(candidate, content_hash="new-hash")
    assert repository.observe(changed, run_id="run-3").state == FindingState.OPEN
```

- [ ] **Step 2: Run tests and confirm missing repository failures**

Run: `uv run pytest tests/test_schema.py tests/test_advisory_repository.py -q`

Expected: FAIL because advisory types, tables, and repository are absent.

- [ ] **Step 3: Define strict types and confidence validation**

```python
class AnalyzerClass(StrEnum):
    DETERMINISTIC = "deterministic"
    MODEL = "model"

class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

class Confidence(StrEnum):
    CONFIRMED = "confirmed"
    LIKELY = "likely"
    SPECULATIVE = "speculative"

class FindingState(StrEnum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    DISMISSED = "dismissed"
    RESOLVED = "resolved"
    REGRESSED = "regressed"

@dataclass(frozen=True)
class FindingEvidence:
    source_ref: str
    observed_at: datetime
    fields: Mapping[str, JSONValue]
    excerpt: str | None = None

@dataclass(frozen=True)
class FindingCandidate:
    analyzer_id: str
    rule_id: str
    target_type: str
    target_id: str
    analyzer_class: AnalyzerClass
    severity: Severity
    confidence: Confidence
    title: str
    impact: str
    remediation: tuple[str, ...]
    evidence: tuple[FindingEvidence, ...]
    content_hash: str | None = None
```

Reject `Confidence.CONFIRMED` when `analyzer_class` is `MODEL`. Compute the stable fingerprint from analyzer, rule, target type, and target ID only.

- [ ] **Step 4: Add advisory tables and implement atomic lifecycle transitions**

Create `advisory_findings` and append-only `advisory_occurrences` exactly as specified. Use one DuckDB transaction for finding upsert plus occurrence insert. Store evidence as bounded structured JSON and short redacted excerpts; reject excerpts over the configured maximum.

- [ ] **Step 5: Run repository tests**

Run: `uv run pytest tests/test_schema.py tests/test_advisory_repository.py -q`

Expected: PASS.

- [ ] **Step 6: Commit the advisory state machine**

```bash
git add src/drover/schema.py src/drover/server/advisory tests/test_schema.py tests/test_advisory_repository.py
git commit -m "feat(advisory): add finding lifecycle store"
```

### Task 2: Deterministic operational analyzers

**Files:**
- Create: `src/drover/server/advisory/analyzers/__init__.py`
- Create: `src/drover/server/advisory/analyzers/connectors.py`
- Create: `src/drover/server/advisory/analyzers/telemetry.py`
- Create: `src/drover/server/advisory/analyzers/hooks.py`
- Create: `src/drover/server/advisory/analyzers/routing.py`
- Test: `tests/test_advisory_analyzers.py`

**Interfaces:**
- Consumes: provider connection state from Plan 1, existing quality/runtime audit queries, spans, sessions, routing, and allowlisted hook descriptors.
- Produces: `Analyzer` protocol and deterministic candidate lists.

- [ ] **Step 1: Write one failing test per rule family**

```python
def test_stale_connector_is_confirmed_high(snapshot):
    finding = ConnectorFreshnessAnalyzer(max_age=timedelta(minutes=15)).analyze(snapshot)[0]
    assert (finding.severity, finding.confidence) == (Severity.HIGH, Confidence.CONFIRMED)

def test_missing_tokens_reports_coverage_not_individual_sessions(snapshot):
    finding = TelemetryCoverageAnalyzer(minimum_percent=80).analyze(snapshot)[0]
    assert finding.rule_id == "telemetry.token_coverage"
    assert finding.evidence[0].fields["coverage_percent"] == 40

def test_missing_hook_executable_is_confirmed(snapshot):
    finding = HookValidityAnalyzer().analyze(snapshot)[0]
    assert finding.remediation[0].startswith("Restore executable")
```

- [ ] **Step 2: Run tests and confirm missing analyzers**

Run: `uv run pytest tests/test_advisory_analyzers.py -q`

Expected: FAIL on missing analyzer modules.

- [ ] **Step 3: Implement the common analyzer protocol and snapshots**

```python
class Analyzer(Protocol):
    analyzer_id: str
    def analyze(self, snapshot: AnalysisSnapshot) -> list[FindingCandidate]: ...
```

`AnalysisSnapshot` is a frozen record containing the source version, analysis
time, provider connection rows, bounded telemetry coverage aggregates, routing
aggregates, and canonical hook descriptors. It contains no arbitrary file
content.

Build immutable snapshots through bounded read-only queries. Analyzers never open arbitrary files; hook validation receives canonical descriptors assembled by the caller.

- [ ] **Step 4: Implement initial deterministic rules**

Cover connector freshness/error, hook executable existence, telemetry flow gaps, repository attribution coverage, token/cost field coverage, routing mismatch frequency, cache-read inefficiency, and contradictory provider reset windows. Every candidate includes numerical evidence and exact non-mutating remediation.

- [ ] **Step 5: Run analyzer and existing quality tests**

Run: `uv run pytest tests/test_advisory_analyzers.py tests/test_quality.py tests/test_runtime_audit.py -q`

Expected: PASS.

- [ ] **Step 6: Commit deterministic analyzers**

```bash
git add src/drover/server/advisory/analyzers tests/test_advisory_analyzers.py
git commit -m "feat(advisory): detect operational configuration gaps"
```

### Task 3: Pipeline-ledger scheduling and analyzer worker

**Files:**
- Create: `src/drover/server/advisory/jobs.py`
- Create: `src/drover/server/advisory/worker.py`
- Modify: `src/drover/server/jobs/__init__.py`
- Modify: `src/drover/server/__main__.py`
- Test: `tests/test_advisory_jobs.py`
- Test: `tests/test_ledger.py`

**Interfaces:**
- Consumes: existing `pipeline_jobs`, `pipeline_job_attempts`, `pipeline_artifacts`, `AdvisoryRepository`, and `Analyzer`.
- Produces: `enqueue_advisory_check()`, `AdvisoryWorker.run_once()`, job kind `analyze_advisory_target`, and artifact kind `advisory_finding_batch`.

- [ ] **Step 1: Write failing source-version and isolation tests**

```python
def test_same_target_hash_coalesces_to_one_job(db_path):
    first = enqueue_advisory_check(db_path, analyzer_id="hooks", target_id="mac", source_version="abc")
    second = enqueue_advisory_check(db_path, analyzer_id="hooks", target_id="mac", source_version="abc")
    assert second.job_id == first.job_id

def test_failed_analyzer_does_not_block_next_analyzer(worker):
    result = worker.run_once([ExplodingAnalyzer(), HealthyAnalyzer()])
    assert result.failed == 1
    assert result.succeeded == 1
```

- [ ] **Step 2: Run tests and confirm missing job/worker failures**

Run: `uv run pytest tests/test_advisory_jobs.py tests/test_ledger.py -q`

Expected: FAIL because advisory job helpers do not exist.

- [ ] **Step 3: Implement coalescing and attempt recording**

Use subject key `analyzer_id:target_id`, source version equal to the target hash or operational snapshot version, and existing ledger lease/retry semantics. Record one attempt per analyzer and one artifact pointing at the affected finding IDs.

- [ ] **Step 4: Wire change-aware and periodic enqueue paths**

Operational changes enqueue lightweight analyzers. A server scheduler enqueues a full deterministic review once per configured interval. `Check Again` uses the same enqueue function with an explicit current source version.

- [ ] **Step 5: Run job and ledger tests**

Run: `uv run pytest tests/test_advisory_jobs.py tests/test_ledger.py -q`

Expected: PASS.

- [ ] **Step 6: Commit scheduler and worker**

```bash
git add src/drover/server/advisory/jobs.py src/drover/server/advisory/worker.py src/drover/server/jobs/__init__.py src/drover/server/__main__.py tests/test_advisory_jobs.py tests/test_ledger.py
git commit -m "feat(advisory): schedule evidence-backed checks"
```

### Task 4: Insights service and authenticated lifecycle API

**Files:**
- Create: `src/drover/server/advisory/service.py`
- Modify: `src/drover/server/metrics.py`
- Modify: `src/drover/server/web/app.py`
- Test: `tests/test_advisory_repository.py`
- Test: `tests/test_metrics.py`

**Interfaces:**
- Produces: `GET /insights`, `GET /insights/{id}`, `POST /insights/{id}/acknowledge`, `POST /insights/{id}/dismiss`, and `POST /insights/{id}/check`.

- [ ] **Step 1: Write failing pagination and action tests**

```python
def test_dismiss_requires_reason(authed_server, finding_id):
    response = authed_server.post_json(f"/insights/{finding_id}/dismiss", {})
    assert response.status == 400

def test_check_again_enqueues_without_mutation(authed_server, finding_id, ledger):
    authed_server.post_json(f"/insights/{finding_id}/check", {})
    assert ledger.jobs(job_kind="analyze_advisory_target")
    assert ledger.configuration_writes == []
```

- [ ] **Step 2: Run tests and confirm 404 failures**

Run: `uv run pytest tests/test_metrics.py -k insights -q`

Expected: FAIL with HTTP 404.

- [ ] **Step 3: Implement strict filters, pagination, and detail serialization**

Allowlist state, severity, confidence, analyzer class, host, harness, and target filters. Use opaque cursor pagination ordered by severity rank, last seen, and finding ID. Detail responses expose bounded evidence and redacted excerpts only.

- [ ] **Step 4: Implement lifecycle POST routes**

Validate IDs and JSON bodies, require dismissal reason, return HTTP 409 for invalid transitions, and enqueue rather than run Check Again synchronously.

- [ ] **Step 5: Run advisory backend verification**

Run: `uv run pytest tests/test_advisory_repository.py tests/test_advisory_analyzers.py tests/test_advisory_jobs.py tests/test_metrics.py -q`

Expected: PASS.

- [ ] **Step 6: Commit the Insights API**

```bash
git add src/drover/server/advisory/service.py src/drover/server/metrics.py src/drover/server/web/app.py tests/test_advisory_repository.py tests/test_metrics.py
git commit -m "feat(advisory): expose insight lifecycle API"
```

## Stage Acceptance

Run: `uv run pytest tests/ -q`

Expected: all Python tests pass. Deterministic findings deduplicate, dismiss, resolve from evidence, and regress correctly; a failed analyzer or absent model backend does not affect Home analytics or fleet APIs.
