# Native Harness Model Catalog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Drover's app-owned model suggestions with host-native, account-scoped Codex, Claude Code, and agy catalogs that update without future iOS releases.

**Architecture:** Each `harnessd` owns bounded native discovery adapters and normalizes their output into a versioned catalog. The central server proxies and persists the last-known-good catalog, while one reusable DroverKit state object decodes, caches, reconciles, and renders model/effort choices for both new and existing sessions.

**Tech Stack:** Python 3.11, dataclasses, `subprocess`, JSON-RPC/HTTP, DuckDB, pytest, Swift 6, Observation, SwiftUI, Swift Testing, XcodeGen.

**Spec:** `docs/plans/2026-08-14-native-model-catalog-design.md`

## Global Constraints

- Ship one bootstrap iOS update; later model and effort additions must be data-only changes.
- Scope discovery to the selected host, harness, authenticated account, effective provider configuration, and policy; never union fleet catalogs.
- Support native discovery for `codex`, `claude-code`, and `agy` in this release.
- Keep model and effort identifiers as opaque strings. Do not add Swift or Python enums containing provider identifiers.
- Always synthesize Harness default and Auto as `null`; never send magic default strings.
- Never restore a hard-coded fallback model list. With no successful discovery, return an empty catalog and keep Harness default usable.
- Keep a five-minute host discovery TTL, persistent central last-known-good data, visible stale metadata, and a forced Retry path.
- Never expose or log credentials, raw account identity, provider bodies, command stderr, or reversible account identifiers.
- A non-null launch or turn preference must be validated against a fresh-enough host catalog; null model and effort remain valid during discovery failures.
- Claude catalog work is accepted only when it matches the installed CLI's `/model` choices for the configured provider/account. Failure degrades to stale/default; it must not introduce a static alias fallback.
- Preserve Claude's startup-only preference lock and the current Codex/agy per-turn mutability behavior.
- Use TDD, run the named failing test before implementation, and commit only the files listed by each task.

## File map

### Python host catalog

- Create `src/drover/server/harness/model_catalog/models.py`: bounded normalized types, wire parsing, and selection validation.
- Create `src/drover/server/harness/model_catalog/scope.py`: host-secret-backed opaque account scope IDs.
- Create `src/drover/server/harness/model_catalog/service.py`: adapter registry, five-minute cache, stale fallback, invalidation, and validation.
- Create `src/drover/server/harness/model_catalog/codex.py`: Codex `model/list` normalization.
- Create `src/drover/server/harness/model_catalog/claude.py`: Claude provider/configuration normalization.
- Create `src/drover/server/harness/model_catalog/agy.py`: bounded `agy models` normalization.
- Create `src/drover/server/harness/model_catalog/__init__.py`: public catalog interfaces and default adapter factory.
- Create `src/drover/server/providers/codex_app_server.py`: reusable bounded Codex app-server transport.
- Create `src/drover/server/providers/claude_credentials.py`: reusable safe Claude credential/account loader.
- Modify `src/drover/server/providers/codex.py`: consume the shared app-server transport.
- Modify `src/drover/server/providers/claude.py`: consume the shared credential loader.
- Modify `src/drover/server/harness/daemon.py`: catalog route, service wiring, cache invalidation, and preference validation.

### Python central server

- Modify `src/drover/server/harness/schema.py`: add `model_catalogs_json` to `harness_hosts` idempotently.
- Modify `src/drover/server/harness/registry.py`: persist and read bounded last-known-good catalogs.
- Modify `src/drover/server/metrics.py`: host catalog proxy, upstream validation, safe degradation, and persistence.
- Modify `src/drover/server/web/app.py`: authenticated public model-catalog route.

### Swift client

- Create `apps/drover/DroverKit/Sources/DroverKit/HarnessModelCatalog.swift`: forward-compatible wire types.
- Create `apps/drover/DroverKit/Sources/DroverKit/HarnessModelCatalogStore.swift`: UserDefaults catalog and preference persistence.
- Create `apps/drover/DroverKit/Sources/DroverKit/HarnessModelCatalogState.swift`: observable loading, refresh, reconciliation, and overrides.
- Create `apps/drover/DroverKit/Sources/DroverKit/HarnessModelCatalogPresentation.swift`: pure display/search/stale helpers.
- Modify `apps/drover/DroverKit/Sources/DroverKit/DroverClient.swift`: catalog request.
- Modify `apps/drover/DroverKit/Sources/DroverKit/HarnessRunPreferences.swift`: retain lifecycle rules only; remove model/effort lists.
- Modify `apps/drover/DroverKit/Sources/DroverKit/LaunchModel.swift`: use shared catalog state.
- Modify `apps/drover/DroverKit/Sources/DroverKit/ChatModel.swift`: use the session host's shared catalog state.
- Create `apps/drover/Drover/Screens/Shared/HarnessModelPicker.swift`: searchable native model sheet.
- Modify `apps/drover/Drover/Screens/Shared/HarnessPreferenceControls.swift`: catalog-driven model and effort controls.
- Modify `apps/drover/Drover/Screens/Shared/GlassPromptSurface.swift`: pass shared catalog state.
- Modify `apps/drover/Drover/Screens/Launch/LaunchView.swift`: load/refresh catalogs and refresh after auth.
- Modify `apps/drover/Drover/Screens/Chat/Composer.swift`: pass shared catalog state.
- Modify `apps/drover/Drover/Screens/Chat/ChatView.swift`: load catalog after session metadata.

---

### Task 1: Normalized catalog contract, scope IDs, and host cache

**Files:**
- Create: `src/drover/server/harness/model_catalog/__init__.py`
- Create: `src/drover/server/harness/model_catalog/models.py`
- Create: `src/drover/server/harness/model_catalog/scope.py`
- Create: `src/drover/server/harness/model_catalog/service.py`
- Test: `tests/test_model_catalog_service.py`

**Interfaces:**
- Consumes: no feature-specific interfaces.
- Produces: `ReasoningOptions`, `ModelOption`, `DiscoveredCatalog`, `CatalogEnvelope`, `CatalogDiscoveryError`, `CatalogSelectionError`, `CatalogAdapter`, `AccountScopeIDs`, and `ModelCatalogService`.
- Produces exact calls used later: `service.read(harness: str, force: bool = False) -> CatalogEnvelope`, `service.validate(harness: str, model: str | None, thinking_effort: str | None) -> None`, and `service.invalidate(harness: str) -> None`.

- [ ] **Step 1: Write failing normalized-contract and service tests**

Create `tests/test_model_catalog_service.py` with fixed UTC clocks and injected adapters. Cover unknown future effort strings, duplicate/default validation, opaque scopes, cache reuse, forced refresh, failure preserving last-good, first failure returning an empty stale envelope, and selection validation:

```python
from datetime import datetime, timedelta, timezone

import pytest

from drover.server.harness.model_catalog import (
    AccountScopeIDs,
    CatalogDiscoveryError,
    CatalogSelectionError,
    DiscoveredCatalog,
    ModelCatalogService,
    ModelOption,
    ReasoningOptions,
)


NOW = datetime(2026, 8, 14, 18, 22, tzinfo=timezone.utc)


class FakeAdapter:
    def __init__(self):
        self.calls = 0
        self.identity = "codex-v1"
        self.failure: str | None = None

    def cache_identity(self) -> str:
        return self.identity

    def discover(self) -> DiscoveredCatalog:
        self.calls += 1
        if self.failure:
            raise CatalogDiscoveryError(self.failure)
        return DiscoveredCatalog(
            account_scope_material="person@example.com|plus",
            harness_version="0.147.0",
            models=(
                ModelOption(
                    id="gpt-5.6-terra",
                    display_name="GPT-5.6 Terra",
                    description="Balanced",
                    is_default=True,
                    reasoning=ReasoningOptions(
                        supported=("low", "medium", "ultra-next"),
                        default="medium",
                    ),
                ),
            ),
        )


def test_catalog_cache_force_refresh_and_stale_fallback():
    clock = [NOW]
    adapter = FakeAdapter()
    service = ModelCatalogService(
        host_id="mac-mini",
        adapters={"codex": adapter},
        scope_ids=AccountScopeIDs(secret=b"x" * 32),
        clock=lambda: clock[0],
        ttl=timedelta(minutes=5),
    )

    first = service.read("codex")
    second = service.read("codex")
    assert adapter.calls == 1
    assert first.models[0].reasoning.supported[-1] == "ultra-next"
    assert second.account_scope_id == first.account_scope_id
    assert "person@example.com" not in first.account_scope_id

    service.read("codex", force=True)
    assert adapter.calls == 2

    adapter.failure = "timeout"
    stale = service.read("codex", force=True)
    assert stale.stale is True
    assert stale.stale_reason == "timeout"
    assert stale.models == first.models


def test_first_failure_returns_empty_stale_catalog_and_defaults_stay_valid():
    adapter = FakeAdapter()
    adapter.failure = "not_authenticated"
    service = ModelCatalogService(
        host_id="mac-mini",
        adapters={"codex": adapter},
        scope_ids=AccountScopeIDs(secret=b"y" * 32),
        clock=lambda: NOW,
    )

    catalog = service.read("codex")
    assert catalog.models == ()
    assert catalog.account_scope_id is None
    assert catalog.discovered_at is None
    assert catalog.stale_reason == "not_authenticated"
    service.validate("codex", None, None)


def test_validation_rejects_unknown_model_and_incompatible_effort():
    service = ModelCatalogService(
        host_id="mac-mini",
        adapters={"codex": FakeAdapter()},
        scope_ids=AccountScopeIDs(secret=b"z" * 32),
        clock=lambda: NOW,
    )
    service.read("codex")

    with pytest.raises(CatalogSelectionError, match="refresh model choices"):
        service.validate("codex", "gpt-missing", None)
    with pytest.raises(CatalogSelectionError, match="not supported"):
        service.validate("codex", "gpt-5.6-terra", "max")
```

Also add constructor tests asserting: successful discovery has a non-empty harness version; IDs are non-empty and at most 256 characters; display names are at most 256; descriptions are at most 2,048; there are at most 256 models; effort lists contain at most 32 unique non-empty strings; a reasoning default belongs to `supported`; model IDs are unique; and at most one model has `is_default=True`.

- [ ] **Step 2: Run the tests and verify the missing package fails**

Run: `.venv/bin/python -m pytest tests/test_model_catalog_service.py -q`

Expected: collection fails with `ModuleNotFoundError: No module named 'drover.server.harness.model_catalog'`.

- [ ] **Step 3: Implement the normalized immutable types and bounded wire format**

In `models.py`, use frozen dataclasses and these exact public fields:

```python
@dataclass(frozen=True)
class ReasoningOptions:
    supported: tuple[str, ...]
    default: str | None = None


@dataclass(frozen=True)
class ModelOption:
    id: str
    display_name: str
    description: str | None = None
    is_default: bool = False
    reasoning: ReasoningOptions | None = None


@dataclass(frozen=True)
class DiscoveredCatalog:
    account_scope_material: str = field(repr=False)
    harness_version: str
    models: tuple[ModelOption, ...]
    source_stale: bool = False


@dataclass(frozen=True)
class CatalogEnvelope:
    host_id: str
    harness: str
    account_scope_id: str | None
    harness_version: str | None
    discovered_at: datetime | None
    stale: bool
    stale_reason: str | None
    models: tuple[ModelOption, ...]

    def to_wire(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "host_id": self.host_id,
            "harness": self.harness,
            "account_scope_id": self.account_scope_id,
            "harness_version": self.harness_version,
            "discovered_at": (
                self.discovered_at.isoformat() if self.discovered_at else None
            ),
            "stale": self.stale,
            "stale_reason": self.stale_reason,
            "models": [model_to_wire(model) for model in self.models],
        }
```

Add `CatalogEnvelope.from_wire(value, expected_host_id, expected_harness)` for the central proxy. It must require schema version 1, exact host/harness equality, valid ISO-8601 timestamps, safe stale reasons, and the bounds listed in Step 1. Discard malformed individual model objects; reject the envelope if a live non-stale response has no valid models, IDs duplicate, or multiple defaults survive normalization. An empty model list is allowed only in a stale degraded envelope. Add `CatalogEnvelope.empty_failure(host_id: str, harness: str, reason: str) -> CatalogEnvelope` to construct that exact null-scope/null-version/null-time degraded envelope.

Define only these safe failure categories:

```python
STALE_REASONS = frozenset(
    {"offline", "timeout", "not_authenticated", "unsupported", "protocol_error"}
)
```

- [ ] **Step 4: Implement opaque scope IDs and the adapter protocol**

In `scope.py`, atomically create or load a 32-byte secret at `~/.drover/model-catalog-scope.key` with mode `0o600`, creating the parent with owner-only permissions when absent; tests inject `secret` and never touch the real home directory. Derive the public ID without retaining the source identity:

```python
class AccountScopeIDs:
    def __init__(self, *, secret: bytes | None = None, path: Path | None = None):
        self._secret = secret if secret is not None else _load_or_create_secret(path)

    def for_material(self, material: str) -> str:
        if not material.strip():
            raise ValueError("account scope material is required")
        return hmac.new(
            self._secret, material.encode("utf-8"), hashlib.sha256
        ).hexdigest()
```

In `service.py`, define:

```python
class CatalogDiscoveryError(RuntimeError):
    def __init__(self, category: str):
        if category not in STALE_REASONS:
            category = "protocol_error"
        super().__init__(category)
        self.category = category


class CatalogSelectionError(ValueError):
    pass


class CatalogAdapter(Protocol):
    def cache_identity(self) -> str:
        raise NotImplementedError

    def discover(self) -> DiscoveredCatalog:
        raise NotImplementedError
```

Implement `ModelCatalogService` with a lock because `ThreadingHTTPServer` may call it concurrently. Cache a successful `CatalogEnvelope` plus adapter identity and expiry per harness. `force=True`, identity changes, and `invalidate()` bypass freshness. A failed refresh returns a copied stale envelope and never overwrites the successful cache. An unsupported/unknown adapter returns an empty stale envelope with reason `unsupported`.

Validation rules are exact: `(None, None)` always passes; explicit selections require a non-stale catalog; model IDs must exist; an explicit effort resolves against the explicit model or the single `is_default` model; and absent/unsupported reasoning raises `CatalogSelectionError` with user-safe text.

- [ ] **Step 5: Export the stable package surface and make tests pass**

In `__init__.py`, re-export only the public names listed in **Interfaces**. Do not export cache entries, scope material, or parsing helpers.

Run: `.venv/bin/python -m pytest tests/test_model_catalog_service.py -q`

Expected: all tests pass.

- [ ] **Step 6: Commit the core contract**

```bash
git add docs/plans/2026-08-14-native-model-catalog-design.md docs/superpowers/plans/2026-08-14-native-model-catalog.md src/drover/server/harness/model_catalog tests/test_model_catalog_service.py
git commit -m "feat: add native model catalog contract"
```

### Task 2: Reusable Codex app-server transport

**Files:**
- Create: `src/drover/server/providers/codex_app_server.py`
- Modify: `src/drover/server/providers/codex.py:1-170`
- Modify: `tests/test_provider_usage.py:88-148,307-430`

**Interfaces:**
- Consumes: existing `redact_auth_text` and the current Codex usage response normalizer.
- Produces: `CodexAppServerError(category: str)` and context-managed `CodexAppServerSession(command: Sequence[str], timeout_s: float)` with `request(method: str, params: Mapping[str, Any] | None) -> Mapping[str, Any]`.

- [ ] **Step 1: Extend the fake app-server and write a failing transport test**

Extend `fake_codex_app_server` so `model/list` returns a page with `data` and `nextCursor`. Add:

```python
def test_codex_app_server_session_initializes_once_and_calls_multiple_methods(
    fake_codex_app_server,
):
    from drover.server.providers.codex_app_server import CodexAppServerSession

    with CodexAppServerSession(fake_codex_app_server.command, timeout_s=1) as client:
        account = client.request("account/read", {"refreshToken": False})
        models = client.request(
            "model/list", {"cursor": None, "includeHidden": False, "limit": 100}
        )

    assert account["account"]["email"] == "person@example.com"
    assert models["data"][0]["model"] == "gpt-5.6-terra"
```

Keep the existing timeout, noisy-stderr, SIGKILL, and broken-stdin tests unchanged; they are regression coverage for the extracted lifecycle.

- [ ] **Step 2: Run the transport test and verify it fails**

Run: `.venv/bin/python -m pytest tests/test_provider_usage.py::test_codex_app_server_session_initializes_once_and_calls_multiple_methods -q`

Expected: FAIL because `codex_app_server.py` does not exist.

- [ ] **Step 3: Extract the bounded process/session implementation**

Move process startup, stdout/stderr drain threads, request ID sequencing, the `initialize` request, `initialized` notification, absolute deadline handling, and best-effort termination from `CodexUsageProbe` into `CodexAppServerSession`. Preserve these behaviors exactly:

```python
class CodexAppServerError(RuntimeError):
    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


class CodexAppServerSession:
    def __enter__(self) -> "CodexAppServerSession":
        self._start()
        initialized = self.request(
            "initialize",
            {"clientInfo": {"name": "drover", "title": "Drover", "version": "0.2.0"}},
        )
        if not isinstance(initialized, Mapping):
            raise CodexAppServerError("protocol_error")
        self.notify("initialized", {})
        return self

    def request(
        self, method: str, params: Mapping[str, Any] | None
    ) -> Mapping[str, Any]:
        request_id = self._next_request_id
        self._next_request_id += 1
        return self._request_with_id(request_id, method, params)
```

The context manager must redact bounded stderr before debug logging, never return it, and never raise from cleanup. It must categorize missing executable, timeout, process failure, and malformed JSON consistently with the existing probe.

- [ ] **Step 4: Rewrite `CodexUsageProbe.read()` over the shared session**

Use one session and keep the existing snapshot/error behavior:

```python
with CodexAppServerSession(self.command, self.timeout_s) as client:
    account_response = client.request("account/read", {"refreshToken": False})
    rate_limit_response = client.request("account/rateLimits/read", None)
return _snapshot_from_responses(
    account_response,
    rate_limit_response,
    host_id=host_id,
    observed_at=observed_at,
)
```

Map `CodexAppServerError.category` into the probe's existing error snapshot. Remove the duplicated private process helpers from `codex.py` after all callers move.

- [ ] **Step 5: Run focused and existing provider tests**

Run: `.venv/bin/python -m pytest tests/test_provider_usage.py -q`

Expected: all tests pass, including the new multi-method test and existing lifecycle regressions.

- [ ] **Step 6: Commit the transport extraction**

```bash
git add src/drover/server/providers/codex_app_server.py src/drover/server/providers/codex.py tests/test_provider_usage.py
git commit -m "refactor: share Codex app-server transport"
```

### Task 3: Codex native catalog adapter

**Files:**
- Create: `src/drover/server/harness/model_catalog/codex.py`
- Modify: `src/drover/server/harness/model_catalog/__init__.py`
- Modify: `src/drover/server/harness/model_catalog/service.py`
- Test: `tests/test_model_catalog_codex.py`

**Interfaces:**
- Consumes: `CodexAppServerSession`, `DiscoveredCatalog`, `ModelOption`, `ReasoningOptions`, and `CatalogDiscoveryError`.
- Produces: `CodexCatalogAdapter(command: Sequence[str], codex_home: Path | None = None, timeout_s: float = 5.0)` implementing `CatalogAdapter`; the default home is `CODEX_HOME` when set and otherwise `~/.codex`.

- [ ] **Step 1: Write failing app-server pagination and native-cache tests**

Create a fake JSONL server that returns account data plus two `model/list` pages and answers the bounded `--version` probe. Assert hidden entries are not emitted, Sol/Terra/Luna preserve ordering, Terra includes `ultra`, Luna does not, and the account email/plan exist only in `account_scope_material`. Add a second test with a failing app-server and a fresh `models_cache.json` fixture under an injected Codex home asserting that native-cache data is returned by the adapter but the service marks it stale.

Use this expected normalization:

```python
assert [model.id for model in discovered.models] == [
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
]
terra = discovered.models[1]
luna = discovered.models[2]
assert terra.reasoning.default == "medium"
assert terra.reasoning.supported[-1] == "ultra"
assert luna.reasoning.supported[-1] == "max"
assert "person@example.com" not in json.dumps(
    [
        {
            "id": model.id,
            "display_name": model.display_name,
            "description": model.description,
        }
        for model in discovered.models
    ]
)
```

- [ ] **Step 2: Run the Codex adapter tests and verify they fail**

Run: `.venv/bin/python -m pytest tests/test_model_catalog_codex.py -q`

Expected: FAIL because `CodexCatalogAdapter` is not defined.

- [ ] **Step 3: Implement live `model/list` pagination and normalization**

The live adapter must call `account/read` and then paginate until `nextCursor` is null:

```python
cursor: str | None = None
items: list[Mapping[str, Any]] = []
while True:
    page = client.request(
        "model/list",
        {"cursor": cursor, "includeHidden": False, "limit": 100},
    )
    data = page.get("data")
    if not isinstance(data, list):
        raise CatalogDiscoveryError("protocol_error")
    items.extend(item for item in data if isinstance(item, Mapping))
    cursor = page.get("nextCursor")
    if cursor is None:
        break
    if not isinstance(cursor, str) or not cursor or len(items) > 256:
        raise CatalogDiscoveryError("protocol_error")
```

Use `model` as the CLI override ID, falling back to `id` only when `model` is absent. Map `displayName`, `description`, `isDefault`, `defaultReasoningEffort`, and each `supportedReasoningEfforts[*].reasoningEffort`. Reject zero valid models. Obtain a non-empty harness version with a separately bounded direct `<codex executable> --version` call. A live result without a version is a `protocol_error`. `cache_identity()` combines executable metadata with stat metadata for `models_cache.json`, `config.toml`, and `auth.json`; it never reads credential contents.

- [ ] **Step 4: Implement bounded native cache fallback with stale provenance**

Read only `models_cache.json` entries whose `visibility` is `list`, map `slug`, `display_name`, `description`, `default_reasoning_level`, and `supported_reasoning_levels[*].effort`, and enforce the same bounds. Require its non-empty `client_version` as the harness version. Set the Task 1 `DiscoveredCatalog.source_stale` field for native-cache results and have `ModelCatalogService` publish that discovery with `stale=True` and reason `offline` while retaining the data.

Do not create any Drover model constants. If both app-server and cache fail, raise the safe category from the app-server failure.

- [ ] **Step 5: Run Codex and core service tests**

Run: `.venv/bin/python -m pytest tests/test_model_catalog_codex.py tests/test_model_catalog_service.py tests/test_provider_usage.py -q`

Expected: all tests pass.

- [ ] **Step 6: Commit the Codex adapter**

```bash
git add src/drover/server/harness/model_catalog tests/test_model_catalog_codex.py
git commit -m "feat: discover Codex model catalog"
```

### Task 4: agy native catalog adapter

**Files:**
- Create: `src/drover/server/harness/model_catalog/agy.py`
- Modify: `src/drover/server/harness/model_catalog/__init__.py`
- Test: `tests/test_model_catalog_agy.py`

**Interfaces:**
- Consumes: core catalog types and an already-resolved agy executable path.
- Produces: `AgyCatalogAdapter(command: Sequence[str], accounts_path: Path | None = None, timeout_s: float = 5.0)` implementing `CatalogAdapter`.

- [ ] **Step 1: Write failing subprocess-output tests**

Use a temporary executable script that prints current `agy models` output. Cover the progress line, tab-separated IDs/names, malformed lines, duplicates, oversized output, timeout, non-zero exit, and account scoping from `google_accounts.json`:

```python
def test_agy_catalog_uses_native_ids_and_omits_separate_reasoning(fake_agy, tmp_path):
    accounts = tmp_path / "google_accounts.json"
    accounts.write_text('{"active":"person@example.com"}')
    adapter = AgyCatalogAdapter(
        command=(str(fake_agy),), accounts_path=accounts, timeout_s=1
    )

    discovered = adapter.discover()

    assert [model.id for model in discovered.models] == [
        "gemini-3.7-flash-high",
        "gemini-3.7-flash-medium",
        "claude-sonnet-4-6",
    ]
    assert all(model.reasoning is None for model in discovered.models)
    assert discovered.account_scope_material == "agy|person@example.com"
```

- [ ] **Step 2: Run the agy adapter tests and verify they fail**

Run: `.venv/bin/python -m pytest tests/test_model_catalog_agy.py -q`

Expected: FAIL because `AgyCatalogAdapter` is not defined.

- [ ] **Step 3: Implement the bounded `agy models` call**

Execute only the registered command plus `models`; do not use `shell=True`:

```python
result = subprocess.run(
    [*self.command, "models"],
    capture_output=True,
    text=True,
    encoding="utf-8",
    errors="replace",
    timeout=self.timeout_s,
    check=False,
)
```

Reject stdout over 256 KiB, non-zero return codes, and zero valid rows. Ignore the literal progress line `Fetching available models...`; every catalog row must split into exactly two non-empty tab-separated fields. Deduplicate by exact ID while preserving CLI order. Never return stderr. Map timeout to `timeout`, missing executable to `unsupported`, and all malformed/process cases to `protocol_error`.

Read only the active account label, falling back to the first valid `old` account label, from the injected accounts file for scope material. If neither exists, return `not_authenticated` rather than publishing an account-ambiguous catalog. `cache_identity()` uses command path/stat plus accounts-file stat. Obtain a non-empty `harness_version` from a separately bounded `agy --version` call; a version failure makes discovery fail with `protocol_error` so a successful envelope never violates the version contract.

- [ ] **Step 4: Run adapter and existing agy tests**

Run: `.venv/bin/python -m pytest tests/test_model_catalog_agy.py tests/test_structured_agy.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit the agy adapter**

```bash
git add src/drover/server/harness/model_catalog/agy.py src/drover/server/harness/model_catalog/__init__.py tests/test_model_catalog_agy.py
git commit -m "feat: discover agy model catalog"
```

### Task 5: Claude authenticated catalog adapter and policy resolver

**Files:**
- Create: `src/drover/server/providers/claude_credentials.py`
- Create: `src/drover/server/harness/model_catalog/claude.py`
- Modify: `src/drover/server/providers/claude.py:1-210`
- Modify: `src/drover/server/harness/model_catalog/__init__.py`
- Modify: `tests/test_claude_usage.py:1-230`
- Test: `tests/test_model_catalog_claude.py`

**Interfaces:**
- Consumes: core catalog types, current no-redirect HTTP behavior, Claude's host-local OAuth/Keychain data, `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`, and documented Claude Code model settings.
- Produces: `ClaudeCredential`, `ClaudeCredentialError`, `load_claude_credential()`, `ClaudeModelPolicy`, and `ClaudeCatalogAdapter` implementing `CatalogAdapter`.

- [ ] **Step 1: Write a failing shared-credential regression test**

Add a direct loader assertion to `tests/test_claude_usage.py` while retaining every existing `ClaudeUsageProbe` test:

```python
def test_shared_credential_loader_returns_identity_without_exposing_token(tmp_path):
    from drover.server.providers.claude_credentials import load_claude_credential

    account = tmp_path / ".claude.json"
    account.write_text(
        '{"oauthAccount":{"accountUuid":"account-123","emailAddress":"person@example.com"}}'
    )
    credential = load_claude_credential(
        credentials_path=_credentials(tmp_path),
        account_path=account,
        keychain_reader=lambda: None,
        now=lambda: datetime(2026, 8, 14, tzinfo=timezone.utc),
    )

    assert credential.access_token == "sk-test-token"
    assert credential.account_identity == "account-123"
    assert credential.subscription_type == "max"
    assert "sk-test-token" not in repr(credential)
```

Declare `access_token` with `repr=False` in the frozen dataclass.

- [ ] **Step 2: Write failing Claude model API and policy tests**

In `tests/test_model_catalog_claude.py`, inject credentials, HTTP opener, environment, settings paths, and command version output. Cover pagination, API-key versus OAuth headers, redirects rejected by the existing no-redirect opener, `availableModels`, dynamic family aliases derived from returned IDs, `modelOverrides`, custom model variables, no known effort metadata, safe errors, and zero static fallback:

```python
def test_claude_catalog_applies_account_policy_without_static_aliases(
    claude_credentials, tmp_path
):
    settings = tmp_path / "settings.json"
    settings.write_text(
        '{"availableModels":["sonnet","claude-fable-5"],'
        '"modelOverrides":{"claude-fable-5":"corp/fable-prod"}}'
    )
    calls = []

    def opener(url, headers, timeout):
        calls.append((url, headers))
        return 200, json.dumps(
            {
                "data": [
                    {"id": "claude-sonnet-5", "display_name": "Sonnet 5"},
                    {"id": "claude-fable-5", "display_name": "Fable 5"},
                    {"id": "claude-opus-5", "display_name": "Opus 5"},
                ],
                "has_more": False,
                "last_id": "claude-opus-5",
            }
        ).encode()

    adapter = ClaudeCatalogAdapter(
        command=("/usr/local/bin/claude",),
        credential_loader=lambda: claude_credentials,
        opener=opener,
        settings_paths=(settings,),
        env={},
        version_reader=lambda command: "2.1.232",
    )

    discovered = adapter.discover()

    assert [model.id for model in discovered.models] == [
        "sonnet",
        "claude-fable-5",
    ]
    assert discovered.models[1].display_name == "Fable 5"
    assert all(model.reasoning is None for model in discovered.models)
    assert calls[0][1]["Authorization"] == "Bearer sk-test-token"
    assert "sk-test-token" not in json.dumps(
        [
            {
                "id": model.id,
                "display_name": model.display_name,
                "description": model.description,
            }
            for model in discovered.models
        ]
    )


def test_claude_discovery_failure_has_no_drover_alias_fallback(
    claude_credentials,
):
    adapter = ClaudeCatalogAdapter(
        command=("claude",),
        credential_loader=lambda: claude_credentials,
        opener=lambda url, headers, timeout: (503, b'{"error":"unavailable"}'),
        settings_paths=(),
        env={},
        version_reader=lambda command: "2.1.232",
    )

    with pytest.raises(CatalogDiscoveryError, match="offline"):
        adapter.discover()
```

- [ ] **Step 3: Run the new Claude tests and verify missing interfaces fail**

Run: `.venv/bin/python -m pytest tests/test_model_catalog_claude.py tests/test_claude_usage.py::test_shared_credential_loader_returns_identity_without_exposing_token -q`

Expected: collection fails because `claude_credentials.py` and `ClaudeCatalogAdapter` do not exist.

- [ ] **Step 4: Extract the credential loader without changing usage behavior**

Create this frozen, redacted value type:

```python
@dataclass(frozen=True)
class ClaudeCredential:
    access_token: str = field(repr=False)
    account_identity: str
    account_label: str
    subscription_type: str | None


class ClaudeCredentialError(RuntimeError):
    def __init__(self, category: str, *, status: str):
        super().__init__(category)
        self.category = category
        self.status = status
```

Move the Keychain-first/file-second OAuth parsing, expiry validation, and account JSON lookup from `ClaudeUsageProbe` into `load_claude_credential`. Preserve current failure precedence and the two-second Keychain bound. `ClaudeUsageProbe` calls the shared loader and maps `ClaudeCredentialError` back to exactly the same snapshots its current tests expect.

Never log, hash, serialize, or return `access_token` outside the host process. `account_identity` prefers `accountUuid`, then email, then organization, and finally the non-secret generic label.

- [ ] **Step 5: Implement Claude policy merging and authenticated model pagination**

`ClaudeModelPolicy.load(settings_paths, env)` must merge arrays in source order with deduplication, let later scalar/maps override earlier values, and expose:

```python
@dataclass(frozen=True)
class ClaudeModelPolicy:
    available_models: tuple[str, ...] | None
    model_overrides: Mapping[str, str]
    custom_model_id: str | None
    custom_model_name: str | None
    custom_model_description: str | None
```

Production settings paths are the readable files among:

```python
(
    Path.home() / ".claude/settings.json",
    Path.home() / ".claude/settings.local.json",
    Path("/Library/Application Support/ClaudeCode/managed-settings.json"),
    Path("/etc/claude-code/managed-settings.json"),
)
```

The adapter uses `ANTHROPIC_API_KEY` with `x-api-key` when present; otherwise it uses the shared OAuth credential with `Authorization: Bearer` and `anthropic-beta: oauth-2025-04-20`. Both send `anthropic-version: 2023-06-01`. Query `GET {ANTHROPIC_BASE_URL or https://api.anthropic.com}/v1/models?limit=1000`, following `has_more` with `after_id=last_id`, and preserve provider ordering. Use the no-redirect HTTP opener. For API-key auth, pass the key plus base URL only as transient `account_scope_material`; the service immediately HMACs it, and the redacted dataclass field prevents accidental repr exposure. For OAuth, use the non-secret account identity plus base URL. Neither raw material may enter logs, exceptions, wire output, or caches.

Build candidate full IDs from the provider response. Derive an alias only from an actually returned canonical ID matching `^claude-([a-z0-9]+)-`; for example, `claude-fable-5` can produce `fable`. This is data-derived and must not contain a checked-in family list. Apply `availableModels` against aliases and canonical IDs before output. `modelOverrides` changes provider routing/display metadata but the selectable ID remains the key Claude Code accepts. Append the configured custom model exactly once.

Only populate `ReasoningOptions` when the provider response explicitly supplies a supported effort array and default. If capability metadata is absent, set `reasoning=None`; never infer effort support from the model name.

- [ ] **Step 6: Add safe categories, identity, and version behavior**

Map 401/403 to `not_authenticated`, timeout to `timeout`, other transport/5xx to `offline`, and malformed/oversized data to `protocol_error`. `cache_identity()` uses the Claude executable stat, settings-file stats, account-file stat, credentials-file stat, base URL, and non-secret environment model keys; it never includes environment secret values or file contents. Read `claude --version` with a bounded direct subprocess and require a non-empty result; a version failure makes discovery fail with `protocol_error` so a successful envelope never violates the version contract.

- [ ] **Step 7: Run Claude adapter, usage, and auth regression tests**

Run: `.venv/bin/python -m pytest tests/test_model_catalog_claude.py tests/test_claude_usage.py tests/test_harness_auth.py -q`

Expected: all tests pass and existing Claude usage/auth behavior is unchanged.

- [ ] **Step 8: Perform the required live Claude parity gate**

On every configured Claude provider path available on the development host:

1. Run `claude` in a trusted directory and open `/model`.
2. Run the adapter through a short read-only Python invocation that prints only `id`, `display_name`, and reasoning metadata—not scope material, headers, or credentials.
3. Compare selectable IDs, restrictions, pinned/custom entries, and effort availability.
4. If they differ, fix the provider/policy normalization and add the mismatch as a fixture before continuing. Do not insert aliases by hand.

Expected: the adapter matches `/model`, or safely returns a discovery failure that the service converts to stale/default. A partial invented list is not acceptable.

- [ ] **Step 9: Commit the Claude adapter and credential extraction**

```bash
git add src/drover/server/providers/claude_credentials.py src/drover/server/providers/claude.py src/drover/server/harness/model_catalog/claude.py src/drover/server/harness/model_catalog/__init__.py tests/test_claude_usage.py tests/test_model_catalog_claude.py
git commit -m "feat: discover Claude model catalog"
```

### Task 6: Harness daemon endpoint, invalidation, and preference validation

**Files:**
- Modify: `src/drover/server/harness/model_catalog/__init__.py`
- Modify: `src/drover/server/harness/daemon.py:1080-1245,1380-1425,1880-2105`
- Modify: `tests/test_harness_daemon.py:1-60,470-710,2450-2760`

**Interfaces:**
- Consumes: all three adapters, `ModelCatalogService`, resolved `HarnessPreset.executable`, and the existing authenticated threaded HTTP handler.
- Produces: authenticated `GET /model-catalog?harness=<name>&refresh=<0|1>`, lazy `HarnessDaemonState.model_catalog_service`, auth invalidation, and launch/turn validation.

- [ ] **Step 1: Write failing daemon route tests with an injected service**

Add a fake service that records reads and validations, then assert authorization, strict query parsing, enabled-harness checks, forced refresh, and response passthrough:

```python
def test_model_catalog_route_is_host_scoped_and_forceable(harness_server):
    state, base_url = harness_server
    state.model_catalog_service = _FakeModelCatalogService()

    status, payload = _json_request(
        f"{base_url}/model-catalog?harness=codex&refresh=1"
    )

    assert status == 200
    assert payload["host_id"] == state.host_id
    assert payload["harness"] == "codex"
    assert state.model_catalog_service.reads == [("codex", True)]
```

Add tests for missing/duplicate harness query, `refresh=2`, disabled harness, no bearer token when auth is configured, and a first discovery failure returning HTTP 200 with an empty stale envelope.

- [ ] **Step 2: Write failing launch and turn validation tests**

For structured session creation, assert invalid model/effort returns 400 before a registry row, worktree, attachment, or driver process exists. For a Codex turn, assert valid IDs reach `StructuredSessionManager.send_turn` unchanged and invalid IDs return 400. Assert `(model=None, thinking_effort=None)` launches even when the fake catalog service raises a discovery failure.

Use the user-facing error text:

```python
assert payload == {
    "error": "gpt-missing is unavailable; refresh model choices and try again"
}
```

- [ ] **Step 3: Run the focused daemon tests and verify they fail**

Run: `.venv/bin/python -m pytest tests/test_harness_daemon.py -q -k 'model_catalog or model_preference_validation'`

Expected: FAIL because the route and validation calls do not exist.

- [ ] **Step 4: Build the default service from resolved presets**

Add this lazy field to `HarnessDaemonState` so tests can inject a fake without touching real credentials:

```python
model_catalog_service: ModelCatalogService | None = None
```

Add `default_model_catalog_service(host_id, presets)` in the package initializer. Register only enabled presets with resolved executables:

- Codex: `(preset.executable, "app-server", "--stdio")`
- Claude Code: `(preset.executable,)`
- agy: `(preset.executable,)`

Create the service on the first catalog read or preference validation and reuse it for the daemon lifetime.

- [ ] **Step 5: Add the authenticated GET route and strict query parser**

In `do_GET`, route `/model-catalog` after `/capabilities`. Parse with `parse_qs(..., keep_blank_values=True)` and require exactly one non-empty `harness`, at most one `refresh`, and refresh in `{"0", "1"}`. Reject unknown/disabled presets with 404. Call `service.read(harness, force=refresh == "1")`, then `_write_json(envelope.to_wire())`.

Do not accept executable paths, provider URLs, account IDs, or arbitrary adapter arguments from the query.

- [ ] **Step 6: Invalidate freshness after auth mutations**

Call `service.invalidate(harness)` after auth start, input, cancel, and any polled flow that reaches `.authenticated`. Invalidation retains last-known-good data; it only forces the next read to rediscover. External sign-ins remain bounded by the five-minute TTL.

- [ ] **Step 7: Validate preferences before side effects**

In `_create_structured_session`, parse `model` and `thinking_effort`, then call:

```python
try:
    self._model_catalog_service().validate(harness, model, thinking_effort)
except CatalogSelectionError as exc:
    self._write_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
    return
```

Place this before command mutation, worktree creation, registry insertion, attachment saving, and driver startup. In `_create_turn`, perform the same validation before saving attachments or calling the driver. Keep Claude's existing rule that later-turn overrides are ignored; do not validate values that are deliberately discarded for Claude.

- [ ] **Step 8: Run daemon and structured-driver regression tests**

Run: `.venv/bin/python -m pytest tests/test_harness_daemon.py tests/test_structured_codex.py tests/test_structured_agy.py tests/test_structured_claude.py -q`

Expected: all tests pass.

- [ ] **Step 9: Commit the harnessd surface**

```bash
git add src/drover/server/harness/model_catalog/__init__.py src/drover/server/harness/daemon.py tests/test_harness_daemon.py
git commit -m "feat: serve and validate native model catalogs"
```

### Task 7: Central last-known-good persistence and proxy

**Files:**
- Modify: `src/drover/server/harness/schema.py:7-30,145-170`
- Modify: `src/drover/server/harness/registry.py:1-300`
- Modify: `src/drover/server/metrics.py:1380-1460,1900-2025`
- Modify: `src/drover/server/web/app.py:770-835`
- Modify: `tests/test_harness_registry.py:1-80,850-885`
- Modify: `tests/test_metrics.py:2520-2710`
- Test: `tests/test_model_catalog_proxy.py`

**Interfaces:**
- Consumes: `CatalogEnvelope.from_wire()`, the existing direct/relay `_harness_request` choke point, and `HarnessRegistry`.
- Produces: `HarnessRegistry.save_model_catalog()`, `HarnessRegistry.latest_model_catalog()`, `MetricsCollector.proxy_harness_model_catalog()`, and public `GET /harness/hosts/{host_id}/model-catalog`.

- [ ] **Step 1: Write failing registry persistence and migration tests**

Add tests proving an existing host table gains `model_catalogs_json` without losing rows, then exercise two account scopes and two harnesses:

```python
def test_model_catalog_cache_is_scoped_and_keeps_latest_per_harness(tmp_path):
    registry, _ = _registry(tmp_path)
    registry.register_host(host_id="mac-mini", display_name="Mac", kind="macos")

    registry.save_model_catalog("mac-mini", "codex", "scope-a", CODEX_CATALOG)
    registry.save_model_catalog("mac-mini", "claude-code", "scope-b", CLAUDE_CATALOG)

    assert registry.latest_model_catalog("mac-mini", "codex") == CODEX_CATALOG
    assert registry.latest_model_catalog("mac-mini", "claude-code") == CLAUDE_CATALOG
```

Also assert unknown hosts raise/return none according to existing registry conventions, malformed stored JSON degrades to none, only schema-version-1 non-stale catalogs can be saved, and each harness retains at most the two most recent scopes.

- [ ] **Step 2: Run registry tests and verify they fail**

Run: `.venv/bin/python -m pytest tests/test_harness_registry.py -q -k 'model_catalog'`

Expected: FAIL because the column and methods do not exist.

- [ ] **Step 3: Add the idempotent host column and bounded registry methods**

Add `model_catalogs_json VARCHAR NOT NULL DEFAULT '{}'` to `_HARNESS_HOSTS_DDL` and `_ensure_harness_columns`. Do not add a new control-plane table or migration key.

Store this bounded JSON shape in the host row:

```json
{
  "codex": {
    "latest_scope_id": "scope-a",
    "scopes": {
      "scope-a": {"schema_version": 1, "host_id": "mac-mini", "harness": "codex"}
    }
  }
}
```

`save_model_catalog(host_id, harness, scope_id, payload)` validates the payload with `CatalogEnvelope.from_wire`, requires `stale=False`, updates under the registry's serialized connection, keeps the newest two scopes for that harness, and caps the serialized column at 512 KiB. `latest_model_catalog` returns a copied dict for the latest scope, never the mutable decoded object.

- [ ] **Step 4: Write failing direct, relay, stale, and empty proxy tests**

In `tests/test_model_catalog_proxy.py`, start a fake harness HTTP server and the central server. Cover:

- successful response is validated, persisted, and returned;
- bearer token is forwarded;
- `refresh=1` is forwarded exactly;
- host/harness path and query values are quoted;
- a later 502/timeout returns the saved catalog with `stale=True` and `stale_reason="offline"`;
- malformed/oversized upstream data cannot overwrite the saved catalog;
- first-ever failure returns schema version 1, null scope/version/time, empty models, and stale metadata;
- unknown host and disabled harness return 404;
- the same behavior works through the fake relay manager.

- [ ] **Step 5: Run the proxy tests and verify they fail**

Run: `.venv/bin/python -m pytest tests/test_model_catalog_proxy.py -q`

Expected: FAIL because the collector and route are missing.

- [ ] **Step 6: Implement the bounded collector proxy and degradation mapping**

Add:

```python
def proxy_harness_model_catalog(
    self, host_id: str, harness: str, *, refresh: bool = False
) -> tuple[int, str]:
```

Verify the host exists and advertises the enabled harness, then call `_harness_request` with a seven-second timeout and 256-KiB response bound:

```python
path = "/model-catalog?" + urlencode(
    {"harness": harness, "refresh": "1" if refresh else "0"}
)
status, body = self._harness_request(
    host,
    path,
    method="GET",
    payload={},
    timeout_s=7.0,
    max_response_bytes=256 * 1024,
)
```

On 2xx, parse JSON, validate with `CatalogEnvelope.from_wire`, and persist only a non-stale successful catalog with a non-null scope. On failure, return the latest persisted catalog copied with `stale=True`; map 401/403 to `not_authenticated`, 404 to `unsupported`, timeout text to `timeout`, unreachable/502 to `offline`, and invalid JSON/schema/size to `protocol_error`. If no cache exists, use `CatalogEnvelope.empty_failure(...)`. Never pass the upstream body or exception string through.

- [ ] **Step 7: Add the authenticated public route**

Recognize only `/harness/hosts/{encoded_host_id}/model-catalog`. Require one `harness` query value and optional `refresh=0|1`; reject duplicates and invalid values with 400. Call the collector method and send JSON. The existing web auth gate must run before the route.

- [ ] **Step 8: Run registry, proxy, relay, and web auth tests**

Run: `.venv/bin/python -m pytest tests/test_harness_registry.py tests/test_model_catalog_proxy.py tests/test_metrics.py tests/test_relay_manager.py tests/test_web_auth.py -q`

Expected: all tests pass.

- [ ] **Step 9: Commit the central API and cache**

```bash
git add src/drover/server/harness/schema.py src/drover/server/harness/registry.py src/drover/server/metrics.py src/drover/server/web/app.py tests/test_harness_registry.py tests/test_metrics.py tests/test_model_catalog_proxy.py
git commit -m "feat: proxy and cache native model catalogs"
```

### Task 8: DroverKit wire types, client, persistence, and shared state

**Files:**
- Create: `apps/drover/DroverKit/Sources/DroverKit/HarnessModelCatalog.swift`
- Create: `apps/drover/DroverKit/Sources/DroverKit/HarnessModelCatalogStore.swift`
- Create: `apps/drover/DroverKit/Sources/DroverKit/HarnessModelCatalogState.swift`
- Create: `apps/drover/DroverKit/Sources/DroverKit/HarnessModelCatalogPresentation.swift`
- Modify: `apps/drover/DroverKit/Sources/DroverKit/DroverClient.swift:130-290`
- Test: `apps/drover/DroverKit/Tests/DroverKitTests/HarnessModelCatalogTests.swift`
- Test: `apps/drover/DroverKit/Tests/DroverKitTests/HarnessModelCatalogStateTests.swift`

**Interfaces:**
- Consumes: public schema-version-1 endpoint and existing `DroverClient` request/decode helpers.
- Produces: `HarnessReasoningOptions`, `HarnessModelOption`, `HarnessModelCatalog`, `HarnessModelCatalogStore`, `HarnessModelCatalogState`, `HarnessModelCatalogPresentation`, and `DroverClient.modelCatalog(hostID:harness:force:)`.

- [ ] **Step 1: Write failing forward-compatible decode and presentation tests**

Create `HarnessModelCatalogTests.swift` using Swift Testing. Assert unknown model IDs, unknown effort strings, unknown JSON fields, null first-success metadata, default lookup, model search, and stale labels all work:

```swift
import Foundation
import Testing
@testable import DroverKit

@Suite struct HarnessModelCatalogTests {
    @Test func decodesUnknownModelsEffortsAndFields() throws {
        let data = Data(#"""
        {
          "schema_version":1,
          "host_id":"mac-mini",
          "harness":"codex",
          "account_scope_id":"scope-a",
          "harness_version":"0.147.0",
          "discovered_at":"2026-08-14T18:22:00Z",
          "stale":false,
          "stale_reason":null,
          "future_top_level":true,
          "models":[{
            "id":"gpt-7-nova",
            "display_name":"GPT-7 Nova",
            "description":"Future model",
            "is_default":true,
            "reasoning":{"supported":["low","galactic"],"default":"galactic"},
            "future_model_field":"kept-compatible"
          }]
        }
        """#.utf8)

        let catalog = try JSONDecoder().decode(HarnessModelCatalog.self, from: data)

        #expect(catalog.models[0].id == "gpt-7-nova")
        #expect(catalog.models[0].reasoning?.supported == ["low", "galactic"])
        #expect(catalog.namedDefault?.id == "gpt-7-nova")
    }

    @Test func neverRefreshedEnvelopeAllowsNullMetadata() throws {
        let data = Data(#"""
        {"schema_version":1,"host_id":"mac-mini","harness":"codex",
         "account_scope_id":null,"harness_version":null,"discovered_at":null,
         "stale":true,"stale_reason":"offline","models":[]}
        """#.utf8)
        let catalog = try JSONDecoder().decode(HarnessModelCatalog.self, from: data)
        #expect(HarnessModelCatalogPresentation.staleText(catalog, now: .now)
            == "Never refreshed")
    }
}
```

- [ ] **Step 2: Write failing persistence and observable-state tests**

Create a fresh `UserDefaults(suiteName:)`, an injected clock, and `MockURLProtocol`. Cover:

- local cached catalog is visible before the network request finishes;
- selections are independent for `mac-mini/codex`, `nas/codex`, and `mac-mini/agy`;
- a changed account scope clears model/effort and records the unavailable message;
- a removed model resets to Harness default;
- changing model clears an unsupported explicit effort;
- out-of-order responses cannot replace the current host/harness;
- transport failure retains the local catalog as stale;
- forced refresh sends `refresh=1`;
- Auto and Harness default expose nil overrides.

Use this core assertion:

```swift
@Test @MainActor func accountChangeResetsAnIncompatibleSelection() async throws {
    let defaults = UserDefaults(suiteName: "catalog-test-\(UUID().uuidString)")!
    let store = HarnessModelCatalogStore(defaults: defaults)
    let state = HarnessModelCatalogState(client: client(), store: store)
    state.select(hostID: "mac-mini", harness: "codex")
    state.apply(fixtureCatalog(scope: "scope-a", model: "gpt-5.6-terra"))
    state.selectedModel = "gpt-5.6-terra"
    state.thinkingEffort = "high"

    state.apply(fixtureCatalog(scope: "scope-b", model: "gpt-6-new"))

    #expect(state.selectedModel.isEmpty)
    #expect(state.thinkingEffort.isEmpty)
    #expect(state.statusMessage == "The previous model is unavailable for this account.")
}
```

- [ ] **Step 3: Run the new Swift tests and verify missing types fail**

Run: `swift test --package-path apps/drover/DroverKit --filter HarnessModelCatalog`

Expected: compilation fails because the catalog types and state do not exist.

- [ ] **Step 4: Implement forward-compatible wire types**

Use public `Sendable`, `Equatable`, `Codable` structs with plain strings:

```swift
public struct HarnessReasoningOptions: Codable, Sendable, Equatable {
    public let supported: [String]
    public let `default`: String?
}

public struct HarnessModelOption: Codable, Sendable, Equatable, Identifiable {
    public let id: String
    public let displayName: String
    public let description: String?
    public let isDefault: Bool
    public let reasoning: HarnessReasoningOptions?

    private enum CodingKeys: String, CodingKey {
        case id
        case displayName = "display_name"
        case description
        case isDefault = "is_default"
        case reasoning
    }
}

public struct HarnessModelCatalog: Codable, Sendable, Equatable {
    public let schemaVersion: Int
    public let hostID: String
    public let harness: String
    public let accountScopeID: String?
    public let harnessVersion: String?
    public let discoveredAt: Date?
    public let stale: Bool
    public let staleReason: String?
    public let models: [HarnessModelOption]
}
```

Give `HarnessModelCatalog` a custom decoder that requires `schema_version == 1`, parses dates through `WireDate`, defaults absent optional fields safely, and does not fail on unknown keys. Add the matching custom encoder so UserDefaults persistence writes the same snake-case wire keys and ISO-8601 date representation. Add `namedDefault`, `model(id:)`, `reasoning(for selectedModel:)`, and `markingStale(reason:)` helpers. The last helper preserves all model data and sets only stale metadata.

- [ ] **Step 5: Implement the authenticated client request**

Add:

```swift
public func modelCatalog(
    hostID: String,
    harness: String,
    force: Bool = false
) async throws -> HarnessModelCatalog {
    let path = "/harness/hosts/\(encodePathComponent(hostID))/model-catalog"
    let url = try queryURL(path: path, items: [
        ("harness", harness),
        ("refresh", force ? "1" : "0"),
    ])
    let data = try await request(url: url, method: "GET", body: nil)
    return try decode(HarnessModelCatalog.self, from: data)
}
```

Tests must assert path/query encoding and normal `DroverError` mapping.

- [ ] **Step 6: Implement bounded UserDefaults persistence**

`HarnessModelCatalogStore` stores one versioned Codable envelope under `drover.model-catalog-store.v1`, containing catalog entries and selection entries keyed by exact host and harness strings. Expose:

```swift
public func catalog(hostID: String, harness: String) -> HarnessModelCatalog?
public func save(catalog: HarnessModelCatalog)
public func selection(hostID: String, harness: String) -> HarnessModelSelection?
public func save(selection: HarnessModelSelection, hostID: String, harness: String)
public func clearSelection(hostID: String, harness: String)
```

`HarnessModelSelection` contains `accountScopeID`, `model`, and `thinkingEffort`. Cap persisted catalogs at 256 models and 256 KiB of encoded JSON; discard a corrupt envelope rather than failing app startup. Do not store credentials or account labels.

- [ ] **Step 7: Implement the shared observable catalog state**

Create the exact public state surface:

```swift
@MainActor
@Observable
public final class HarnessModelCatalogState {
    public private(set) var hostID = ""
    public private(set) var harness = ""
    public private(set) var catalog: HarnessModelCatalog?
    public private(set) var isRefreshing = false
    public private(set) var statusMessage: String?
    public var selectedModel = "" { didSet { selectionDidChange() } }
    public var thinkingEffort = "" { didSet { selectionDidChange() } }

    public var modelOverride: String? { normalized(selectedModel) }
    public var thinkingEffortOverride: String? { normalized(thinkingEffort) }

    public func select(
        hostID: String,
        harness: String,
        seedModel: String? = nil,
        seedThinkingEffort: String? = nil
    )
    public func refresh(force: Bool = false) async
    public func apply(_ catalog: HarnessModelCatalog)
}
```

`select` increments a generation, loads the matching cached catalog immediately, then restores only a selection whose saved scope equals the cached scope. Explicit session seeds take precedence over saved preferences. `refresh` captures the generation/key, calls the client, discards late results, and keeps cached data marked stale on transport failure. `apply` verifies host/harness, detects scope changes, reconciles model and effort, persists state, and never sends a removed identifier.

When Harness default is selected, effort capabilities come from the single named default; if none exists, clear explicit effort. Auto remains the empty string. Use a private reentrancy guard so reconciliation assignments do not recursively save half-updated selections.

- [ ] **Step 8: Implement pure presentation helpers**

`HarnessModelCatalogPresentation` provides:

```swift
public static func filteredModels(
    in catalog: HarnessModelCatalog?, query: String
) -> [HarnessModelOption]
public static func modelTitle(selection: String, catalog: HarnessModelCatalog?) -> String
public static func effortTitle(selection: String, catalog: HarnessModelCatalog?) -> String
public static func staleText(_ catalog: HarnessModelCatalog, now: Date) -> String?
public static func title(forRawEffort effort: String) -> String
```

Search display name, exact ID, and description case-insensitively. Unknown effort strings become readable by replacing `-`/`_` with spaces and capitalizing words, while the state keeps the raw value for submission. Stale text is `Never refreshed` when `discoveredAt` is null and otherwise `Last updated <relative time>`.

- [ ] **Step 9: Run DroverKit catalog tests**

Run: `swift test --package-path apps/drover/DroverKit --filter HarnessModelCatalog`

Expected: all catalog decode, persistence, state, request, and presentation tests pass.

- [ ] **Step 10: Commit the DroverKit catalog foundation**

```bash
git add apps/drover/DroverKit/Sources/DroverKit/HarnessModelCatalog.swift apps/drover/DroverKit/Sources/DroverKit/HarnessModelCatalogStore.swift apps/drover/DroverKit/Sources/DroverKit/HarnessModelCatalogState.swift apps/drover/DroverKit/Sources/DroverKit/HarnessModelCatalogPresentation.swift apps/drover/DroverKit/Sources/DroverKit/DroverClient.swift apps/drover/DroverKit/Tests/DroverKitTests/HarnessModelCatalogTests.swift apps/drover/DroverKit/Tests/DroverKitTests/HarnessModelCatalogStateTests.swift
git commit -m "feat: add dynamic model catalog state"
```

### Task 9: Launch and chat model integration

**Files:**
- Modify: `apps/drover/DroverKit/Sources/DroverKit/HarnessRunPreferences.swift:1-32`
- Modify: `apps/drover/DroverKit/Sources/DroverKit/LaunchModel.swift:1-135`
- Modify: `apps/drover/DroverKit/Sources/DroverKit/ChatModel.swift:1-220,520-655`
- Modify: `apps/drover/DroverKit/Tests/DroverKitTests/HarnessRunPreferencesTests.swift`
- Modify: `apps/drover/DroverKit/Tests/DroverKitTests/LaunchModelTests.swift`
- Modify: `apps/drover/DroverKit/Tests/DroverKitTests/ChatModelTests.swift`

**Interfaces:**
- Consumes: `HarnessModelCatalogState` and existing session metadata.
- Produces: `LaunchModel.runPreferences` and `ChatModel.runPreferences`, both using the same catalog/selection semantics.

- [ ] **Step 1: Rewrite launch tests first and verify the old model fails them**

Inject a `HarnessModelCatalogStore` backed by a test UserDefaults suite. Replace direct `selectedModel`/`thinkingEffort` setup with `model.runPreferences`. Add tests that switching host/harness calls `select`, restores only that pair's preference, and launch sends nil for default/auto:

```swift
@Test @MainActor func launchUsesCatalogStateOverrides() async throws {
    let model = LaunchModel(client: client(), snapshot: try snapshot(), store: testStore())
    model.harness = "codex"
    model.runPreferences.apply(codexCatalog())
    model.runPreferences.selectedModel = "gpt-5.6-terra"
    model.runPreferences.thinkingEffort = "high"

    MockURLProtocol.handler = { request in
        let body = try! JSONSerialization.jsonObject(
            with: request.bodyStreamData()) as! [String: Any]
        #expect(body["model"] as? String == "gpt-5.6-terra")
        #expect(body["thinking_effort"] as? String == "high")
        return (201, Data(#"{"session_id":"harness-pref"}"#.utf8))
    }

    #expect(await model.launch() == "harness-pref")
}
```

Run: `swift test --package-path apps/drover/DroverKit --filter LaunchModelTests`

Expected: compilation fails because `runPreferences` and the store injection do not exist.

- [ ] **Step 2: Integrate `LaunchModel` with shared state**

Add `public let runPreferences: HarnessModelCatalogState`. Initialize it with the same client/store, then call `select(hostID:harness:)` after initial host/harness resolution. Host and harness observers call `select` after maintaining valid harness selection. `launch()` submits `runPreferences.modelOverride` and `runPreferences.thinkingEffortOverride` only for structured sessions.

Remove `selectedModel`, `thinkingEffort`, `supportsThinkingEffort`, `thinkingEfforts`, and `modelSuggestions` from the launch path. Keep `HarnessRunPreferences.canChangeInExistingSession` and its lifecycle test; keep `optional` only if another caller still needs it after the migration.

- [ ] **Step 3: Rewrite chat tests before changing `ChatModel`**

Update metadata and send tests to assert:

- session host/harness select the catalog key;
- stored preferences do not override non-empty session metadata;
- a catalog refresh removes unsupported values before a turn;
- Codex sends valid model/effort strings unchanged;
- agy sends its selected model and no separate effort when catalog metadata has none;
- Claude later-turn overrides remain nil and controls remain locked.

Run: `swift test --package-path apps/drover/DroverKit --filter ChatModelTests`

Expected: compilation fails at the new `runPreferences` assertions.

- [ ] **Step 4: Integrate `ChatModel` with session-scoped catalog state**

Add `public let runPreferences: HarnessModelCatalogState`, constructed from the client and injected store. In `applySessionMetadata`, call:

```swift
runPreferences.select(
    hostID: session.hostID,
    harness: session.harness,
    seedModel: session.model,
    seedThinkingEffort: session.thinkingEffort
)
```

At the end of `loadSessionMetadata()`, await `runPreferences.refresh()` after applying metadata. Replace `turnPreferences` with the state's normalized overrides only when `HarnessRunPreferences.canChangeInExistingSession(harness)` is true. Remove direct `selectedModel` and `thinkingEffort` fields.

- [ ] **Step 5: Run focused model tests**

Run: `swift test --package-path apps/drover/DroverKit --filter LaunchModelTests`

Run: `swift test --package-path apps/drover/DroverKit --filter ChatModelTests`

Run: `swift test --package-path apps/drover/DroverKit --filter HarnessRunPreferencesTests`

Expected: all tests pass and no static model/effort list remains in DroverKit.

- [ ] **Step 6: Commit model-layer integration**

```bash
git add apps/drover/DroverKit/Sources/DroverKit/HarnessRunPreferences.swift apps/drover/DroverKit/Sources/DroverKit/LaunchModel.swift apps/drover/DroverKit/Sources/DroverKit/ChatModel.swift apps/drover/DroverKit/Tests/DroverKitTests/HarnessRunPreferencesTests.swift apps/drover/DroverKit/Tests/DroverKitTests/LaunchModelTests.swift apps/drover/DroverKit/Tests/DroverKitTests/ChatModelTests.swift
git commit -m "feat: scope model preferences by host and harness"
```

### Task 10: Searchable SwiftUI picker and stale/default UX

**Files:**
- Create: `apps/drover/Drover/Screens/Shared/HarnessModelPicker.swift`
- Modify: `apps/drover/Drover/Screens/Shared/HarnessPreferenceControls.swift:1-75`
- Modify: `apps/drover/Drover/Screens/Shared/GlassPromptSurface.swift:1-105`
- Modify: `apps/drover/Drover/Screens/Launch/LaunchView.swift:1-150`
- Modify: `apps/drover/Drover/Screens/Chat/Composer.swift:1-85`
- Modify: `apps/drover/Drover/Screens/Chat/ChatView.swift:118-160`
- Test: `apps/drover/DroverTests/HarnessModelPickerTests.swift`

**Interfaces:**
- Consumes: `HarnessModelCatalogState` and pure presentation helpers.
- Produces: model chip, searchable catalog sheet, catalog-derived effort menu, stale status, Retry, and auth-dismiss refresh.

- [ ] **Step 1: Write failing app-level presentation tests**

Test the non-visual decisions the SwiftUI views consume: Harness default is always first, descriptions and IDs remain searchable, named-default effort is used for Harness default, a model without reasoning hides the effort chip, and stale/never-refreshed status produces Retry copy. Keep assertions in `HarnessModelPickerTests.swift` so the generated iOS target compiles the same types the view uses.

Run: `xcodegen generate`

Working directory: `apps/drover`

Resolve the first available iPhone simulator and run only the picker tests:

```bash
SIMULATOR_ID="$(xcrun simctl list devices available | awk -F '[()]' '/iPhone/ { print $2; exit }')"
test -n "$SIMULATOR_ID"
xcodebuild -project Drover.xcodeproj -scheme Drover -destination "id=$SIMULATOR_ID" -only-testing:DroverTests/HarnessModelPickerTests test
```

Working directory: `apps/drover`

Expected: compilation fails because `HarnessModelPicker` and the catalog-driven controls do not exist.

- [ ] **Step 2: Implement the searchable model sheet**

`HarnessModelPicker` takes a `HarnessModelCatalogState` and editability flag. Its list contains:

1. A permanent Harness default row that sets `selectedModel = ""`.
2. Filtered native model rows showing display name, exact ID when different, and optional description.
3. A footer with ProgressView during refresh, `Never refreshed`/`Last updated …` for stale data, safe status text, and a Retry button that calls `await state.refresh(force: true)`.

Use `.searchable(text:)`, plain strings from the catalog, and accessibility labels containing both display name and exact ID. Do not sort provider results; preserve native order.

- [ ] **Step 3: Replace fixed menus with catalog-driven controls**

Change `HarnessPreferenceControls` to receive:

```swift
let runPreferences: HarnessModelCatalogState
let isEditable: Bool
```

The model chip presents `HarnessModelPicker`. The effort chip appears only when `runPreferences.catalog?.reasoning(for: selectedModel)` is non-null. Its menu starts with Auto (`thinkingEffort = ""`) followed by every raw supported value. Titles use the presentation helper; tags/submissions retain raw strings. The chip label displays `Auto (<Default>)` when a default exists.

Keep the current lock icon, disabled opacity, compact chip styling, and accessibility semantics.

- [ ] **Step 4: Thread the shared state through launch and chat views**

Replace the two string bindings in `GlassPromptSurface` and `Composer` with `runPreferences`. Pass `model.runPreferences` from `LaunchView` and `ChatView`.

In `LaunchView`, add:

```swift
.task(id: "\(model.hostID)\u{1f}\(model.harness)") {
    await model.runPreferences.refresh()
}
```

Give the auth sheet an `onDismiss` callback that runs `await model.runPreferences.refresh(force: true)`. In chat, `loadSessionMetadata()` already performs the scoped refresh; do not launch a competing second request.

- [ ] **Step 5: Build and run the picker tests**

Run: `xcodegen generate`

Working directory: `apps/drover`

Resolve the first available iPhone simulator and run only the picker tests:

```bash
SIMULATOR_ID="$(xcrun simctl list devices available | awk -F '[()]' '/iPhone/ { print $2; exit }')"
test -n "$SIMULATOR_ID"
xcodebuild -project Drover.xcodeproj -scheme Drover -destination "id=$SIMULATOR_ID" -only-testing:DroverTests/HarnessModelPickerTests test
```

Working directory: `apps/drover`

Expected: tests pass and the app target compiles with the new source file.

- [ ] **Step 6: Commit the bootstrap UI**

```bash
git add apps/drover/Drover/Screens/Shared/HarnessModelPicker.swift apps/drover/Drover/Screens/Shared/HarnessPreferenceControls.swift apps/drover/Drover/Screens/Shared/GlassPromptSurface.swift apps/drover/Drover/Screens/Launch/LaunchView.swift apps/drover/Drover/Screens/Chat/Composer.swift apps/drover/Drover/Screens/Chat/ChatView.swift apps/drover/DroverTests/HarnessModelPickerTests.swift
git commit -m "feat: show native harness model picker"
```

### Task 11: Contract audit, full verification, and live acceptance

**Files:**
- Correct only the exact feature files listed in Tasks 1-10 when a verification step exposes a defect.
- Verify: `docs/plans/2026-08-14-native-model-catalog-design.md`
- Verify: `docs/superpowers/plans/2026-08-14-native-model-catalog.md`

**Interfaces:**
- Consumes: the complete feature.
- Produces: evidence that static fallback data is gone, all automated suites pass, all three native adapters match their harness, stale fallback works, and the bootstrap iOS app builds.

- [ ] **Step 1: Audit the source for forbidden static model/effort lists and credential leakage**

Run: `rg -n 'gpt-5\\.6-(sol|terra|luna)|gemini-3\\.[0-9]|claude-(sonnet|opus|fable)|thinkingEfforts|modelSuggestions' src apps/drover --glob '!**/*Tests.swift' --glob '!**/tests/**'`

Expected: model IDs appear only in protocol fixtures/comments that document observed data, never in app/server fallback arrays. `thinkingEfforts` and `modelSuggestions` have no production definitions.

Run: `rg -n 'access_token|Authorization|x-api-key' src/drover/server/harness/model_catalog src/drover/server/providers/claude_credentials.py`

Expected: secrets occur only in credential/header construction code and are never included in `to_wire`, repr output, exceptions, or logging.

- [ ] **Step 2: Run formatting and diff hygiene**

Run: `.venv/bin/black --check src/drover/server/harness/model_catalog src/drover/server/providers/codex_app_server.py src/drover/server/providers/claude_credentials.py tests/test_model_catalog_service.py tests/test_model_catalog_codex.py tests/test_model_catalog_claude.py tests/test_model_catalog_agy.py tests/test_model_catalog_proxy.py`

Expected: exit 0.

Run: `git diff --check`

Expected: no output and exit 0.

- [ ] **Step 3: Run the complete Python suite**

Run: `.venv/bin/python -m pytest -q`

Expected: all tests pass.

- [ ] **Step 4: Run the complete DroverKit suite**

Run: `swift test --package-path apps/drover/DroverKit`

Expected: all tests pass.

- [ ] **Step 5: Run the generated iOS unit-test suite**

Run: `xcodegen generate`

Working directory: `apps/drover`

Resolve an available simulator exactly as CI does:

```bash
SIMULATOR_ID="$(xcrun simctl list devices available | awk -F '[()]' '/iPhone/ { print $2; exit }')"
test -n "$SIMULATOR_ID"
xcodebuild -project Drover.xcodeproj -scheme Drover -destination "id=$SIMULATOR_ID" test
```

Working directory: `apps/drover`

Expected: all iOS unit tests pass.

- [ ] **Step 6: Verify live native catalogs before deployment**

Set `DROVER_SERVER_URL` to the active hub base URL and `DROVER_API_TOKEN` to an already-issued test/device credential in the shell environment. For each online host/harness pair, run:

```bash
curl -fsS \
  -H "Authorization: Bearer ${DROVER_API_TOKEN:?set DROVER_API_TOKEN}" \
  "${DROVER_SERVER_URL:?set DROVER_SERVER_URL}/harness/hosts/mac-mini/model-catalog?harness=codex&refresh=1" \
  | jq '{harness,stale,stale_reason,models:[.models[]|{id,reasoning}]}'
```

Repeat with `harness=claude-code` and `harness=agy`. Compare Codex output to `codex app-server`/`~/.codex/models_cache.json`, Claude output to `/model`, and agy output to `agy models`. Expected on the current Codex host: Sol, Terra, and Luna appear; Terra includes `ultra`; Luna ends at `max`.

- [ ] **Step 7: Verify last-known-good degradation**

After one successful refresh in a test environment, inject an upstream timeout using the proxy test fixture or stop only the disposable test harnessd. Request the same catalog again.

Expected: HTTP 200, identical model IDs, `stale=true`, safe stale reason, and unchanged `discovered_at`. For a host/harness with no stored success, expect an empty model array and null discovery metadata. Restore the test harness immediately after the check.

- [ ] **Step 8: Build, install, and manually smoke-test the bootstrap app**

Run: `scripts/deploy-ios.sh --device iPhone`

Expected: build, install, and launch succeed on the paired phone.

On device verify:

- changing host/harness changes the catalog without cross-host leakage;
- Codex shows Sol, Terra, and Luna with model-specific effort choices;
- Claude and agy match their native choices;
- Harness default and Auto submit no overrides;
- Retry refreshes stale data;
- signing into a different account invalidates the old selection;
- an existing Claude session remains locked;
- a new native model appears after server refresh without rebuilding the app.

- [ ] **Step 9: Commit any evidence-driven corrections and the approved docs**

If Steps 1-8 required code corrections, rerun the affected focused test before this commit. Inspect `git status --short`, then stage only the corrected feature files from the following allowlist (omit every unchanged path):

```bash
git add src/drover/server/harness/model_catalog/__init__.py src/drover/server/harness/model_catalog/models.py src/drover/server/harness/model_catalog/scope.py src/drover/server/harness/model_catalog/service.py src/drover/server/harness/model_catalog/codex.py src/drover/server/harness/model_catalog/claude.py src/drover/server/harness/model_catalog/agy.py src/drover/server/providers/codex_app_server.py src/drover/server/providers/claude_credentials.py src/drover/server/providers/codex.py src/drover/server/providers/claude.py src/drover/server/harness/daemon.py src/drover/server/harness/schema.py src/drover/server/harness/registry.py src/drover/server/metrics.py src/drover/server/web/app.py tests/test_model_catalog_service.py tests/test_model_catalog_codex.py tests/test_model_catalog_claude.py tests/test_model_catalog_agy.py tests/test_model_catalog_proxy.py tests/test_provider_usage.py tests/test_claude_usage.py tests/test_harness_daemon.py tests/test_harness_registry.py tests/test_metrics.py apps/drover/DroverKit/Sources/DroverKit/HarnessModelCatalog.swift apps/drover/DroverKit/Sources/DroverKit/HarnessModelCatalogStore.swift apps/drover/DroverKit/Sources/DroverKit/HarnessModelCatalogState.swift apps/drover/DroverKit/Sources/DroverKit/HarnessModelCatalogPresentation.swift apps/drover/DroverKit/Sources/DroverKit/DroverClient.swift apps/drover/DroverKit/Sources/DroverKit/HarnessRunPreferences.swift apps/drover/DroverKit/Sources/DroverKit/LaunchModel.swift apps/drover/DroverKit/Sources/DroverKit/ChatModel.swift apps/drover/DroverKit/Tests/DroverKitTests/HarnessModelCatalogTests.swift apps/drover/DroverKit/Tests/DroverKitTests/HarnessModelCatalogStateTests.swift apps/drover/DroverKit/Tests/DroverKitTests/HarnessRunPreferencesTests.swift apps/drover/DroverKit/Tests/DroverKitTests/LaunchModelTests.swift apps/drover/DroverKit/Tests/DroverKitTests/ChatModelTests.swift apps/drover/Drover/Screens/Shared/HarnessModelPicker.swift apps/drover/Drover/Screens/Shared/HarnessPreferenceControls.swift apps/drover/Drover/Screens/Shared/GlassPromptSurface.swift apps/drover/Drover/Screens/Launch/LaunchView.swift apps/drover/Drover/Screens/Chat/Composer.swift apps/drover/Drover/Screens/Chat/ChatView.swift apps/drover/DroverTests/HarnessModelPickerTests.swift
git commit -m "test: verify native model catalog rollout"
```

If there are no evidence-driven corrections, do not create an empty verification commit. The two approved documents were already included in Task 1's core-contract commit.

- [ ] **Step 10: Final repository checks**

Run: `git status --short`

Expected: clean, except for pre-existing unrelated user files identified before execution.

Run: `git log --oneline --decorate -12`

Expected: review-sized commits for the core contract, shared transports, three adapters, harness endpoint, central proxy/cache, DroverKit state, model integration, UI, and any evidence-driven correction.
