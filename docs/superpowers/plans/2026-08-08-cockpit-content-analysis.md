# Cockpit Opt-In Content Analysis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add explicitly consented analysis of system prompts, hooks, prompt patterns, and skills while persisting only derived findings, hashes, references, and short redacted excerpts.

**Architecture:** Each host reads only configured allowlisted targets, resolves paths safely, redacts secrets, and returns a bounded ephemeral bundle over the authenticated private Drover channel. The central advisory worker sends that bundle to the selected local or disclosed external backend, validates structured model output, stores derived candidates, and drops the bundle.

**Tech Stack:** Python 3.11, TOML configuration, existing harness relay, existing Anthropic/Ollama summarizer backends, DuckDB pipeline ledger, pytest.

## Global Constraints

- Content analysis is disabled by default.
- Local analysis is the default backend policy.
- External analysis requires a separate explicit consent flag.
- Full files and prompt bundles are never written to DuckDB, Parquet, raw objects, logs, attempts, or artifacts.
- Canonical path and symlink resolution must keep reads within explicit allowlisted roots/files.
- Enforce per-file and per-bundle byte limits before backend submission.
- Model findings may be `likely` or `speculative`, never `confirmed`.
- Revocation stops future model jobs; excerpt purge does not delete lifecycle metadata.

---

## File Structure

- `src/drover/config.py`: advisory consent, backend policy, target allowlists, and size limits.
- `src/drover/server/advisory/content_targets.py`: safe target resolution, hashing, and bundle assembly.
- `src/drover/server/advisory/redaction.py`: secret-pattern and structured credential redaction.
- `src/drover/server/advisory/model_analyzer.py`: prompt construction, backend call, and output validation.
- `src/drover/server/advisory/prompt.py`: stable JSON-only analysis prompt.
- `src/drover/server/harness/daemon.py`: authenticated ephemeral analysis-bundle endpoint.
- `src/drover/server/advisory/worker.py`: model job execution and consent fence.
- `src/drover/server/advisory/service.py`: consent status and excerpt purge.
- `tests/test_advisory_content.py`: path, redaction, size, and ephemerality tests.
- `tests/test_advisory_model.py`: structured output, uncertainty, and consent tests.
- `tests/test_config.py`: defaults and parsing.
- `tests/test_metrics.py`: authenticated purge and bundle routes.

### Task 1: Consent configuration and safe content bundling

**Files:**
- Modify: `src/drover/config.py`
- Modify: `src/drover/server/__main__.py`
- Create: `src/drover/server/advisory/content_targets.py`
- Create: `src/drover/server/advisory/redaction.py`
- Test: `tests/test_config.py`
- Test: `tests/test_advisory_content.py`

**Interfaces:**
- Produces: `AdvisoryContentConfig`, `ContentTarget`, `ContentBundle`, `build_content_bundle()`, and `redact_content()`.

- [ ] **Step 1: Write failing default-deny and traversal tests**

```python
def test_content_analysis_defaults_to_disabled():
    cfg = default_config().advisory_content
    assert cfg.enabled is False
    assert cfg.backend_policy == "local"
    assert cfg.external_consent is False

def test_bundle_rejects_symlink_escape(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (allowed / "escape").symlink_to(tmp_path / "secret")
    with pytest.raises(ContentTargetError, match="outside allowlist"):
        build_content_bundle([ContentTarget(allowed / "escape")], allowed_roots=[allowed])
```

- [ ] **Step 2: Run tests and confirm missing config/bundle failures**

Run: `uv run pytest tests/test_config.py tests/test_advisory_content.py -q`

Expected: FAIL because advisory content configuration and bundling do not exist.

- [ ] **Step 3: Add exact configuration keys and defaults**

```toml
[advisory_content]
enabled = false
backend_policy = "local"
external_consent = false
targets = []
allowed_roots = []
max_file_bytes = 131072
max_bundle_bytes = 524288
excerpt_max_chars = 320
```

Parse these into a frozen `AdvisoryContentConfig`. Reject `backend_policy="cloud"` unless `external_consent=true`.

- [ ] **Step 4: Implement canonical reads, hashing, and redaction**

Resolve each target and parent root, reject escapes and non-regular files, read no more than the configured byte cap, decode UTF-8 strictly, redact credential-shaped JSON/TOML keys and token patterns, then hash the redacted content. Abort the entire bundle if its aggregate cap would be exceeded.

```python
@dataclass(frozen=True)
class ContentBundle:
    host_id: str
    created_at: datetime
    targets: tuple[BundledTarget, ...]
    bundle_hash: str
```

- [ ] **Step 5: Run privacy-focused tests**

Run: `uv run pytest tests/test_config.py tests/test_advisory_content.py -q`

Expected: PASS for traversal, symlinks, oversized files, invalid UTF-8, secret redaction, stable hashes, and disabled defaults.

- [ ] **Step 6: Commit consent and bundling**

```bash
git add src/drover/config.py src/drover/server/__main__.py src/drover/server/advisory/content_targets.py src/drover/server/advisory/redaction.py tests/test_config.py tests/test_advisory_content.py
git commit -m "feat(advisory): add consented content bundles"
```

### Task 2: Host-local ephemeral bundle endpoint

**Files:**
- Modify: `src/drover/server/harness/daemon.py`
- Modify: `src/drover/server/metrics.py`
- Test: `tests/test_harness_daemon.py`
- Test: `tests/test_metrics.py`

**Interfaces:**
- Consumes: `build_content_bundle()` and configured allowlists from Task 1.
- Produces: host `POST /advisory/content-bundle` and central relay method `fetch_advisory_content_bundle(host_id, target_ids)`.

- [ ] **Step 1: Write failing authorization and non-persistence tests**

```python
def test_bundle_endpoint_requires_host_auth(harnessd):
    assert harnessd.post("/advisory/content-bundle", {}).status == 401

def test_bundle_endpoint_does_not_create_payload_files(harnessd, drover_home):
    response = harnessd.authed_post("/advisory/content-bundle", {"target_ids": ["global-agents"]})
    assert response.status == 200
    assert not list(drover_home.rglob("*bundle*"))
```

- [ ] **Step 2: Run tests and confirm missing endpoint failures**

Run: `uv run pytest tests/test_harness_daemon.py tests/test_metrics.py -k 'content_bundle' -q`

Expected: FAIL with HTTP 404.

- [ ] **Step 3: Implement bounded host endpoint**

Accept target IDs, not arbitrary paths. Resolve IDs against server-start configuration, return `{bundle_hash, created_at, targets:[{target_id, content_hash, redacted_content}]}`, add `Cache-Control: no-store`, and clear in-memory references after serialization.

- [ ] **Step 4: Add central direct/relay proxy with no logging of body content**

Reuse existing host routing and relay request mechanics. Logs may include host ID, target count, byte count, and bundle hash only.

- [ ] **Step 5: Run endpoint tests**

Run: `uv run pytest tests/test_harness_daemon.py tests/test_metrics.py -k 'content_bundle' -q`

Expected: PASS.

- [ ] **Step 6: Commit ephemeral transport**

```bash
git add src/drover/server/harness/daemon.py src/drover/server/metrics.py tests/test_harness_daemon.py tests/test_metrics.py
git commit -m "feat(advisory): transport ephemeral analysis bundles"
```

### Task 3: Structured model analyzer and consent-fenced worker

**Files:**
- Create: `src/drover/server/advisory/prompt.py`
- Create: `src/drover/server/advisory/model_analyzer.py`
- Modify: `src/drover/server/advisory/worker.py`
- Test: `tests/test_advisory_model.py`
- Test: `tests/test_advisory_jobs.py`

**Interfaces:**
- Consumes: `ContentBundle`, existing Anthropic/Ollama backend selection, `FindingCandidate`, and advisory jobs.
- Produces: `ModelConfigurationAnalyzer.analyze(bundle) -> list[FindingCandidate]`.

- [ ] **Step 1: Write failing structured-output and consent tests**

```python
def test_model_output_cannot_claim_confirmed(fake_backend, bundle):
    fake_backend.response = '{"findings":[{"confidence":"confirmed"}]}'
    with pytest.raises(ModelFindingError, match="confirmed"):
        ModelConfigurationAnalyzer(fake_backend).analyze(bundle)

def test_disabled_content_job_never_fetches_bundle(worker, bundle_fetcher):
    worker.run_model_job(content_enabled=False)
    bundle_fetcher.assert_not_called()
```

- [ ] **Step 2: Run tests and confirm missing analyzer failures**

Run: `uv run pytest tests/test_advisory_model.py tests/test_advisory_jobs.py -q`

Expected: FAIL because model analysis is absent.

- [ ] **Step 3: Implement a JSON-only prompt and strict parser**

Require rule ID, target ID, likely/speculative confidence, severity, impact, bounded evidence excerpt, and ordered remediation. Reject extra top-level data, unknown targets, confirmed confidence, mutation actions, and excerpts not present in the redacted bundle.

- [ ] **Step 4: Reuse configured backends without reusing summarizer prompts**

Select the existing local Ollama or Anthropic transport through a narrow `AnalysisBackend.complete(system, user) -> str` adapter. Enforce local policy by construction. Cloud backend creation fails closed unless external consent is true.

- [ ] **Step 5: Fence job execution and discard bundle references**

Re-read consent immediately before fetch and before backend call. Record only bundle/target hashes and finding IDs in attempt metrics/artifacts. Drop bundle variables in `finally`; never include backend request content in exception text.

- [ ] **Step 6: Run model and job tests**

Run: `uv run pytest tests/test_advisory_model.py tests/test_advisory_jobs.py tests/test_summarizer_client.py -q`

Expected: PASS.

- [ ] **Step 7: Commit model analysis**

```bash
git add src/drover/server/advisory/prompt.py src/drover/server/advisory/model_analyzer.py src/drover/server/advisory/worker.py tests/test_advisory_model.py tests/test_advisory_jobs.py
git commit -m "feat(advisory): analyze opted-in configuration content"
```

### Task 4: Consent status, revocation, and excerpt purge API

**Files:**
- Modify: `src/drover/server/advisory/service.py`
- Modify: `src/drover/server/web/app.py`
- Test: `tests/test_advisory_repository.py`
- Test: `tests/test_metrics.py`

**Interfaces:**
- Produces: `GET /insights/content-analysis`, `POST /insights/content-analysis/consent`, `POST /insights/content-analysis/revoke`, and `DELETE /insights/content-excerpts`.

- [ ] **Step 1: Write failing revocation and purge tests**

```python
def test_revoke_cancels_pending_model_jobs_but_keeps_findings(service):
    service.revoke_content_analysis()
    assert service.pending_model_jobs() == []
    assert service.list_findings(analyzer_class="model")

def test_purge_removes_excerpts_not_occurrence_metadata(service):
    count = service.purge_content_excerpts()
    assert count == 2
    assert service.occurrences()[0].excerpt is None
    assert service.occurrences()[0].content_hash == "hash-1"
```

- [ ] **Step 2: Run tests and confirm missing API/service failures**

Run: `uv run pytest tests/test_advisory_repository.py tests/test_metrics.py -k 'content_analysis or excerpt' -q`

Expected: FAIL.

- [ ] **Step 3: Implement explicit consent transitions**

Consent payload names `local` or `cloud`; cloud also requires `external_disclosure_accepted=true`. Revocation disables configuration and cancels pending model jobs without deleting findings.

- [ ] **Step 4: Implement transactional excerpt purge**

Set excerpt fields to null while retaining finding ID, run ID, source references, content hash, timestamps, and structured non-content evidence. Return the affected occurrence count.

- [ ] **Step 5: Run content-analysis verification**

Run: `uv run pytest tests/test_advisory_content.py tests/test_advisory_model.py tests/test_advisory_repository.py tests/test_metrics.py -q`

Expected: PASS.

- [ ] **Step 6: Commit privacy controls**

```bash
git add src/drover/server/advisory/service.py src/drover/server/web/app.py tests/test_advisory_repository.py tests/test_metrics.py
git commit -m "feat(advisory): add content privacy controls"
```

## Stage Acceptance

Run: `uv run pytest tests/ -q`

Expected: all Python tests pass. With default configuration, no content is read or sent. Local consent produces model-class findings without persisting full content. Cloud use requires separate consent, and revocation plus excerpt purge behaves independently from lifecycle history.
