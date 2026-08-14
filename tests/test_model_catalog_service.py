from datetime import datetime, timedelta, timezone

import pytest

from drover.server.harness.model_catalog import (
    AccountScopeIDs,
    CatalogDiscoveryError,
    CatalogEnvelope,
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


@pytest.mark.parametrize(
    "factory",
    [
        lambda: DiscoveredCatalog(
            account_scope_material="scope", harness_version="", models=()
        ),
        lambda: ModelOption(id="", display_name="Model"),
        lambda: ModelOption(id="m" * 257, display_name="Model"),
        lambda: ModelOption(id="model", display_name="x" * 257),
        lambda: ModelOption(id="model", display_name="Model", description="x" * 2049),
        lambda: ReasoningOptions(supported=("",)),
        lambda: ReasoningOptions(supported=("low", "low")),
        lambda: ReasoningOptions(supported=tuple(str(i) for i in range(33))),
        lambda: ReasoningOptions(supported=("low",), default="medium"),
    ],
)
def test_constructors_reject_invalid_bounded_values(factory):
    with pytest.raises(ValueError):
        factory()


def test_discovered_catalog_rejects_too_many_duplicate_or_multiple_default_models():
    model = ModelOption(id="model", display_name="Model")
    with pytest.raises(ValueError):
        DiscoveredCatalog(
            account_scope_material="scope",
            harness_version="1",
            models=tuple(model for _ in range(257)),
        )
    with pytest.raises(ValueError):
        DiscoveredCatalog(
            account_scope_material="scope",
            harness_version="1",
            models=(model, model),
        )
    with pytest.raises(ValueError):
        DiscoveredCatalog(
            account_scope_material="scope",
            harness_version="1",
            models=(
                ModelOption(id="one", display_name="One", is_default=True),
                ModelOption(id="two", display_name="Two", is_default=True),
            ),
        )


def test_wire_parser_discards_bad_models_but_rejects_bad_live_catalogs():
    envelope = CatalogEnvelope(
        host_id="mac-mini",
        harness="codex",
        account_scope_id="opaque",
        harness_version="1",
        discovered_at=NOW,
        stale=False,
        stale_reason=None,
        models=(ModelOption(id="model", display_name="Model"),),
    )
    value = envelope.to_wire()
    value["models"] = [
        {"id": "", "display_name": "bad"},
        {"id": "model", "display_name": "Model"},
    ]

    parsed = CatalogEnvelope.from_wire(value, "mac-mini", "codex")
    assert [model.id for model in parsed.models] == ["model"]

    value["models"] = [{"id": "", "display_name": "bad"}]
    with pytest.raises(ValueError):
        CatalogEnvelope.from_wire(value, "mac-mini", "codex")


def test_wire_parser_requires_exact_identity_and_safe_degraded_shape():
    failure = CatalogEnvelope.empty_failure("mac-mini", "codex", "timeout")
    assert failure.to_wire()["models"] == []
    assert failure.to_wire()["account_scope_id"] is None
    assert failure.to_wire()["discovered_at"] is None

    value = failure.to_wire()
    value["stale"] = False
    with pytest.raises(ValueError):
        CatalogEnvelope.from_wire(value, "mac-mini", "codex")
    value["stale"] = True
    value["host_id"] = "other-host"
    with pytest.raises(ValueError):
        CatalogEnvelope.from_wire(value, "mac-mini", "codex")


def test_identity_change_and_invalidation_bypass_a_fresh_cache():
    adapter = FakeAdapter()
    service = ModelCatalogService(
        host_id="mac-mini",
        adapters={"codex": adapter},
        scope_ids=AccountScopeIDs(secret=b"q" * 32),
        clock=lambda: NOW,
    )

    service.read("codex")
    adapter.identity = "codex-v2"
    service.read("codex")
    service.invalidate("codex")
    service.read("codex")

    assert adapter.calls == 3
