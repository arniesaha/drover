# Cockpit Provider and Analytics Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver provider subscription inventory, Codex rate-limit snapshots, cross-host activity analytics, and authenticated Home/Analytics APIs.

**Architecture:** Host daemons own provider credentials and query provider-native surfaces. The central server persists normalized provider snapshots beside existing telemetry and composes provider-reported capacity with Drover-observed analytics without reconciling the two. Codex is the first full quota adapter via the documented app-server account protocol; detected Claude and Gemini installations return explicit `usage_unavailable` records until they expose a stable machine-readable quota contract.

**Tech Stack:** Python 3.11, stdlib HTTP/JSON-RPC, DuckDB, PyArrow/Parquet, existing Drover harness relay, pytest.

## Global Constraints

- Provider connectors are authoritative for plan identity, usage windows, remaining capacity, and reset times.
- Never infer subscription capacity or reset time from OTLP spans, token totals, session events, or published plan limits.
- AgentWeave is optional; missing spans lower analytics coverage without disabling provider cards or fleet state.
- Persist append-oriented provider snapshots in Parquet and mutable connector status in DuckDB.
- Return partial section status instead of failing the complete Home response.
- Keep all new HTTP endpoints behind the existing bearer-token gate.
- Codex protocol reference: `https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md` (`account/read`, `account/rateLimits/read`, sparse `account/rateLimits/updated`).
- Gemini `/stats model` is session-scoped and is not used as a subscription snapshot.

---

## File Structure

- `src/drover/server/providers/types.py`: normalized provider account/window records and wire serialization.
- `src/drover/server/providers/codex.py`: bounded Codex app-server JSON-RPC probe.
- `src/drover/server/providers/inventory.py`: map harness capabilities to supported or unavailable provider accounts.
- `src/drover/server/providers/service.py`: central refresh, persistence, freshness, and latest-snapshot reads.
- `src/drover/server/cockpit/analytics.py`: observed activity queries and coverage calculation.
- `src/drover/server/cockpit/service.py`: compose Home and Analytics responses.
- `src/drover/schema.py`: provider Parquet view and `provider_connections` table.
- `src/drover/server/harness/daemon.py`: authenticated host-local provider-usage endpoint.
- `src/drover/server/metrics.py`: delegate cockpit rendering to `CockpitService`.
- `src/drover/server/web/app.py`: authenticated `/cockpit/overview` and `/analytics` routing.
- `tests/test_provider_usage.py`: normalization, Codex probing, inventory, persistence, and freshness.
- `tests/test_cockpit_analytics.py`: project rankings, filters, coverage, and fallback behavior.
- `tests/test_metrics.py`: HTTP authentication and partial-response integration.

### Task 1: Provider storage and normalized contracts

**Files:**
- Create: `src/drover/server/providers/__init__.py`
- Create: `src/drover/server/providers/types.py`
- Modify: `src/drover/schema.py`
- Test: `tests/test_schema.py`
- Test: `tests/test_provider_usage.py`

**Interfaces:**
- Produces: `ProviderUsageWindow`, `ProviderAccountSnapshot`, `provider_snapshot_table()`, DuckDB view `provider_usage_snapshots`, and table `provider_connections`.
- Consumes: `atomic_write_table()` from `drover.server.parquet_io` in Task 3.

- [ ] **Step 1: Write failing contract and bootstrap tests**

```python
def test_provider_window_rejects_negative_percent():
    with pytest.raises(ValueError, match="used_percent"):
        ProviderUsageWindow(kind="primary", used_percent=-1, resets_at=None)

def test_bootstrap_creates_provider_storage(tmp_lakehouse):
    parquet_dir, db_path = tmp_lakehouse
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db_path)
    assert (parquet_dir / "provider_usage_snapshots").is_dir()
    con = duckdb.connect(str(db_path))
    assert con.execute("SELECT count(*) FROM provider_usage_snapshots").fetchone() == (0,)
    assert con.execute("SELECT count(*) FROM provider_connections").fetchone() == (0,)
```

- [ ] **Step 2: Run the focused tests and confirm the missing types/storage failure**

Run: `uv run pytest tests/test_schema.py tests/test_provider_usage.py -q`

Expected: FAIL because `providers.types`, the Parquet directory/view, and `provider_connections` do not exist.

- [ ] **Step 3: Implement strict normalized records**

```python
@dataclass(frozen=True)
class ProviderUsageWindow:
    kind: str
    used_percent: float | None
    limit_value: float | None = None
    remaining_value: float | None = None
    unit: str | None = None
    window_minutes: int | None = None
    starts_at: datetime | None = None
    resets_at: datetime | None = None

@dataclass(frozen=True)
class ProviderAccountSnapshot:
    snapshot_id: str
    dedup_key: str
    provider: str
    account_label: str
    plan_label: str | None
    host_id: str
    status: Literal["ok", "usage_unavailable", "stale", "error"]
    observed_at: datetime
    windows: tuple[ProviderUsageWindow, ...]
    source: str
    error_category: str | None = None
```

Validate percentages in `[0, 100]`, require timezone-aware timestamps, and preserve multiple windows instead of merging them.

- [ ] **Step 4: Add the Parquet schema, empty view, and mutable connection table**

Add `provider_usage_snapshots` to `PARQUET_SUBDIRS` and `EXPECTED_VIEWS`. Add `provider_connections` to `EXPECTED_TABLES` with `provider`, `account_label`, `host_id`, `enabled`, capability flags, last-attempt/success timestamps, error category, and credential reference. Follow the existing typed-empty-view pattern so a new lakehouse boots before its first snapshot.

- [ ] **Step 5: Run schema and contract tests**

Run: `uv run pytest tests/test_schema.py tests/test_provider_usage.py -q`

Expected: PASS.

- [ ] **Step 6: Commit the storage contract**

```bash
git add src/drover/schema.py src/drover/server/providers tests/test_schema.py tests/test_provider_usage.py
git commit -m "feat(cockpit): add provider usage storage contract"
```

### Task 2: Host-local provider inventory and Codex quota adapter

**Files:**
- Create: `src/drover/server/providers/inventory.py`
- Create: `src/drover/server/providers/codex.py`
- Modify: `src/drover/server/harness/daemon.py`
- Test: `tests/test_provider_usage.py`
- Test: `tests/test_harness_daemon.py`

**Interfaces:**
- Consumes: `ProviderAccountSnapshot` and `ProviderUsageWindow` from Task 1.
- Produces: `detect_provider_accounts(capabilities) -> list[DetectedProvider]`, `CodexUsageProbe.read() -> ProviderAccountSnapshot`, and authenticated `GET /providers/usage` on harnessd.

- [ ] **Step 1: Write failing JSON-RPC and unavailable-provider tests**

```python
def test_codex_probe_reads_plan_and_multiple_windows(fake_codex_app_server):
    snapshot = CodexUsageProbe(command=fake_codex_app_server.command).read()
    assert snapshot.provider == "openai"
    assert snapshot.plan_label == "plus"
    assert [(w.kind, w.used_percent) for w in snapshot.windows] == [
        ("primary", 25.0), ("secondary", 60.0)
    ]

def test_detected_gemini_is_honest_when_quota_contract_is_unavailable():
    accounts = detect_provider_accounts({"harnesses": [{"id": "gemini"}]})
    assert accounts[0].provider == "google"
    assert accounts[0].usage_status == "usage_unavailable"
```

- [ ] **Step 2: Run tests and confirm missing adapter failures**

Run: `uv run pytest tests/test_provider_usage.py tests/test_harness_daemon.py -q`

Expected: FAIL because inventory, the Codex probe, and the endpoint are absent.

- [ ] **Step 3: Implement a bounded Codex app-server probe**

Launch `codex app-server --stdio`, send `initialize`, `account/read`, and `account/rateLimits/read`, and terminate the child in `finally`. Parse `planType`, `primary`, and `secondary`; ignore absent windows; convert Unix `resetsAt` seconds to UTC. Use a hard overall timeout and redact all stderr before returning an error category.

```python
class CodexUsageProbe:
    def __init__(self, command: Sequence[str] = ("codex", "app-server", "--stdio"), timeout_s: float = 5.0): ...
    def read(self, *, host_id: str = "local") -> ProviderAccountSnapshot: ...
```

Define inventory records explicitly:

```python
@dataclass(frozen=True)
class DetectedProvider:
    provider: str
    account_label: str
    host_id: str
    harnesses: tuple[str, ...]
    plan_label: str | None
    usage_status: Literal["supported", "usage_unavailable"]
```

- [ ] **Step 4: Implement inventory fallbacks and host endpoint**

`detect_provider_accounts()` maps available `codex`, `claude-code`, and `gemini` presets to OpenAI, Anthropic, and Google records. Only Codex invokes a full adapter. Claude auth detail may populate `plan_label`, but its quota remains unavailable. Gemini remains unavailable rather than converting documented daily plan limits into live usage.

Add `GET /providers/usage` beside `/capabilities`; require host auth and return `{accounts, observed_at}`.

- [ ] **Step 5: Run provider and daemon tests**

Run: `uv run pytest tests/test_provider_usage.py tests/test_harness_daemon.py -q`

Expected: PASS, including process timeout/cleanup and redacted errors.

- [ ] **Step 6: Commit the host adapter**

```bash
git add src/drover/server/providers src/drover/server/harness/daemon.py tests/test_provider_usage.py tests/test_harness_daemon.py
git commit -m "feat(harness): report provider subscription usage"
```

### Task 3: Central snapshot refresh and observed analytics

**Files:**
- Create: `src/drover/server/providers/service.py`
- Create: `src/drover/server/cockpit/__init__.py`
- Create: `src/drover/server/cockpit/analytics.py`
- Test: `tests/test_provider_usage.py`
- Test: `tests/test_cockpit_analytics.py`

**Interfaces:**
- Consumes: host `/providers/usage`, `provider_snapshot_table()`, `atomic_write_table()`, `spans_enriched`, `sessions`, and `harness_sessions`.
- Produces: `ProviderUsageService.refresh_host()`, `ProviderUsageService.latest_accounts()`, and `activity_analytics(con, filters) -> ActivityAnalytics`.

- [ ] **Step 1: Write failing persistence, freshness, and ranking tests**

```python
def test_last_good_provider_snapshot_survives_refresh_failure(service, host):
    service.refresh_host(host, fetch=lambda _: GOOD_PAYLOAD)
    service.refresh_host(host, fetch=lambda _: (_ for _ in ()).throw(TimeoutError()))
    account = service.latest_accounts()[0]
    assert account.status == "stale"
    assert account.windows[0].used_percent == 25.0

def test_project_ranking_falls_back_when_token_coverage_is_low(analytics_db):
    result = activity_analytics(analytics_db, AnalyticsFilters(days=7))
    assert result.project_metric == "sessions"
    assert result.coverage.token_percent < 80
```

- [ ] **Step 2: Run tests and confirm service/query failures**

Run: `uv run pytest tests/test_provider_usage.py tests/test_cockpit_analytics.py -q`

Expected: FAIL because refresh, latest reads, analytics filters, and coverage do not exist.

- [ ] **Step 3: Implement atomic snapshot persistence and stale projection**

Write one Parquet part per host refresh using a `.parquet.tmp` atomic rename. Use snapshot `dedup_key` to suppress identical observations. Update `provider_connections` on every attempt. `latest_accounts()` selects the last successful record per provider/account/host and overlays stale/error connector state without deleting its windows.

- [ ] **Step 4: Implement bounded analytics queries**

```python
@dataclass(frozen=True)
class AnalyticsFilters:
    days: int = 7
    host_id: str | None = None
    harness: str | None = None
    provider: str | None = None
    model: str | None = None
    project_key: str | None = None

def activity_analytics(con: duckdb.DuckDBPyConnection, filters: AnalyticsFilters) -> ActivityAnalytics: ...
```

`ActivityAnalytics` contains totals, project/harness/host/model breakdowns,
`project_metric: Literal["tokens", "sessions"]`, and a `Coverage` record with
attributable-session, token, cost, cache, and latency percentages.

Aggregate tokens, cost, cache, latency, and sessions from existing normalized views. Calculate token coverage as attributable sessions with tokens divided by attributable sessions. Use tokens only at `>= 80%`; otherwise rank by session count and emit `project_metric="sessions"`.

- [ ] **Step 5: Run focused tests and the existing attribution suite**

Run: `uv run pytest tests/test_provider_usage.py tests/test_cockpit_analytics.py tests/test_spans_view_attribution.py -q`

Expected: PASS.

- [ ] **Step 6: Commit the central services**

```bash
git add src/drover/server/providers/service.py src/drover/server/cockpit tests/test_provider_usage.py tests/test_cockpit_analytics.py
git commit -m "feat(cockpit): aggregate provider and project analytics"
```

### Task 4: Authenticated cockpit APIs and runtime refresh

**Files:**
- Create: `src/drover/server/cockpit/service.py`
- Modify: `src/drover/server/metrics.py`
- Modify: `src/drover/server/web/app.py`
- Modify: `src/drover/server/__main__.py`
- Test: `tests/test_metrics.py`
- Test: `tests/test_cockpit_analytics.py`

**Interfaces:**
- Consumes: `ProviderUsageService` and `activity_analytics()` from Task 3.
- Produces: `CockpitService.overview(filters)`, `CockpitService.analytics(filters)`, `GET /cockpit/overview`, and `GET /analytics`.

- [ ] **Step 1: Write failing authenticated API tests**

```python
def test_cockpit_overview_returns_partial_sections(authed_server):
    payload = authed_server.get_json("/cockpit/overview?days=7")
    assert payload["provider_capacity"]["status"] == "stale"
    assert payload["activity"]["status"] == "ok"
    assert payload["popular_projects"][0]["metric"] in {"tokens", "sessions"}

def test_analytics_requires_auth(server):
    with pytest.raises(HTTPError) as exc:
        urlopen(server.url + "/analytics")
    assert exc.value.code == 401
```

- [ ] **Step 2: Run endpoint tests and confirm 404 failures**

Run: `uv run pytest tests/test_metrics.py -k 'cockpit or analytics_requires' -q`

Expected: FAIL with HTTP 404.

- [ ] **Step 3: Add composed service and route delegation**

Parse only allowlisted query fields and reject invalid days/ranges with HTTP 400. `CockpitService.overview()` catches failures per section and returns `{status, observed_at, coverage, data}` envelopes. Add `CockpitService | None` to `MetricsCollector`; wire it from configured DuckDB and Parquet paths in server startup.

Add `cockpit_api_version: 1` and supported section names to the existing
`/harness` capability payload. Older servers omit the field; older clients
ignore it.

- [ ] **Step 4: Add a bounded periodic provider refresh loop**

Start one daemon thread with the server, refresh online hosts no more than once per five minutes, and stop it through the existing shutdown event. A refresh exception updates connection state and never exits the server.

- [ ] **Step 5: Run backend verification**

Run: `uv run pytest tests/test_provider_usage.py tests/test_cockpit_analytics.py tests/test_metrics.py tests/test_web_auth.py -q`

Expected: PASS.

Run: `uv run black --check src/drover/server/providers src/drover/server/cockpit tests/test_provider_usage.py tests/test_cockpit_analytics.py`

Expected: PASS.

- [ ] **Step 6: Commit the API slice**

```bash
git add src/drover/server/cockpit/service.py src/drover/server/metrics.py src/drover/server/web/app.py src/drover/server/__main__.py tests/test_metrics.py tests/test_cockpit_analytics.py
git commit -m "feat(cockpit): expose usage analytics APIs"
```

## Stage Acceptance

Run: `uv run pytest tests/ -q`

Expected: all Python tests pass. A live authenticated `GET /cockpit/overview` returns useful fleet/activity data with zero provider adapters, detected-but-unavailable Claude/Gemini entries, and full provider windows when Codex app-server is authenticated.
