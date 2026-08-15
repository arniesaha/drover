from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import logging

import pytest

from drover.server.harness.model_catalog import (
    MAX_CATALOG_WIRE_BYTES,
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


def _sized_discovery(target_wire_bytes: int) -> DiscoveredCatalog:
    material = "catalog-boundary-scope"
    secret = b"w" * 32
    descriptions = ["x" for _ in range(256)]

    def payload() -> dict[str, object]:
        return {
            "schema_version": 1,
            "host_id": "mac-mini",
            "harness": "codex",
            "account_scope_id": hmac.new(
                secret, material.encode("utf-8"), hashlib.sha256
            ).hexdigest(),
            "harness_version": "1.0",
            "discovered_at": NOW.isoformat(),
            "stale": False,
            "stale_reason": None,
            "models": [
                {
                    "id": f"model-{index}",
                    "display_name": f"Model {index}",
                    "description": description,
                    "is_default": index == 0,
                    "reasoning": None,
                }
                for index, description in enumerate(descriptions)
            ],
        }

    remaining = target_wire_bytes - len(
        json.dumps(payload(), sort_keys=True).encode("utf-8")
    )
    assert remaining >= 0
    for index in range(len(descriptions)):
        added = min(remaining, 2_047)
        descriptions[index] += "x" * added
        remaining -= added
    assert remaining == 0
    assert (
        len(json.dumps(payload(), sort_keys=True).encode("utf-8")) == target_wire_bytes
    )
    return DiscoveredCatalog(
        account_scope_material=material,
        harness_version="1.0",
        models=tuple(
            ModelOption(
                id=f"model-{index}",
                display_name=f"Model {index}",
                description=description,
                is_default=index == 0,
            )
            for index, description in enumerate(descriptions)
        ),
    )


class SizedAdapter:
    def __init__(self, discovered: DiscoveredCatalog):
        self.discovered = discovered

    def cache_identity(self) -> str:
        return "sized-v1"

    def discover(self) -> DiscoveredCatalog:
        return self.discovered


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


def test_discovered_catalog_scope_secret_does_not_affect_identity_or_repr():
    models = (ModelOption(id="model", display_name="Model"),)
    first = DiscoveredCatalog(
        account_scope_material="scope-secret-one",
        harness_version="1",
        models=models,
    )
    second = DiscoveredCatalog(
        account_scope_material="scope-secret-two",
        harness_version="1",
        models=models,
    )

    assert first == second
    assert hash(first) == hash(second)
    assert "scope-secret-one" not in repr(first)
    assert "scope-secret-two" not in repr(second)


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


def test_discovery_failure_logs_harness_and_safe_stale_reason(caplog):
    adapter = FakeAdapter()
    adapter.failure = "timeout"
    service = ModelCatalogService(
        host_id="mac-mini",
        adapters={"codex": adapter},
        scope_ids=AccountScopeIDs(secret=b"l" * 32),
        clock=lambda: NOW,
    )

    with caplog.at_level(logging.WARNING, logger="drover.model_catalog"):
        catalog = service.read("codex")

    assert catalog.stale_reason == "timeout"
    record = next(
        item
        for item in caplog.records
        if item.name == "drover.model_catalog"
        and item.getMessage()
        == "model catalog discovery failed harness=codex stale_reason=timeout"
    )
    assert record.harness == "codex"
    assert record.stale_reason == "timeout"
    assert "person@example.com" not in record.getMessage()


def test_unexpected_discovery_failure_logs_no_exception_detail(caplog):
    class UnexpectedFailureAdapter(FakeAdapter):
        def discover(self) -> DiscoveredCatalog:
            raise RuntimeError("gateway-secret tenant@example.com")

    service = ModelCatalogService(
        host_id="mac-mini",
        adapters={"codex": UnexpectedFailureAdapter()},
        scope_ids=AccountScopeIDs(secret=b"l" * 32),
        clock=lambda: NOW,
    )

    with caplog.at_level(logging.WARNING, logger="drover.model_catalog"):
        catalog = service.read("codex")

    assert catalog.stale_reason == "protocol_error"
    assert (
        "model catalog discovery failed harness=codex stale_reason=protocol_error"
        in caplog.text
    )
    assert "gateway-secret" not in caplog.text
    assert "tenant@example.com" not in caplog.text


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


def test_source_stale_refresh_does_not_replace_fresh_last_known_good_catalog():
    class StagedAdapter:
        def __init__(self):
            self.stage = "fresh"

        def cache_identity(self) -> str:
            return "codex-v1"

        def discover(self) -> DiscoveredCatalog:
            if self.stage == "failure":
                raise CatalogDiscoveryError("timeout")
            return DiscoveredCatalog(
                account_scope_material="person@example.com|plus",
                harness_version="0.147.0",
                source_stale=self.stage == "source-stale",
                models=(
                    ModelOption(
                        id=(
                            "fresh-model"
                            if self.stage == "fresh"
                            else "stale-native-cache-model"
                        ),
                        display_name="Catalog model",
                    ),
                ),
            )

    adapter = StagedAdapter()
    service = ModelCatalogService(
        host_id="mac-mini",
        adapters={"codex": adapter},
        scope_ids=AccountScopeIDs(secret=b"s" * 32),
        clock=lambda: NOW,
    )

    fresh = service.read("codex")
    adapter.stage = "source-stale"
    source_stale = service.read("codex", force=True)
    adapter.stage = "failure"
    fallback = service.read("codex", force=True)

    assert fresh.models[0].id == "fresh-model"
    assert source_stale.stale is True
    assert source_stale.models[0].id == "stale-native-cache-model"
    assert fallback.stale_reason == "timeout"
    assert fallback.models == fresh.models


def test_host_accepts_catalog_just_below_canonical_wire_cap():
    adapter = SizedAdapter(_sized_discovery(MAX_CATALOG_WIRE_BYTES - 1))
    service = ModelCatalogService(
        host_id="mac-mini",
        adapters={"codex": adapter},
        scope_ids=AccountScopeIDs(secret=b"w" * 32),
        clock=lambda: NOW,
    )

    catalog = service.read("codex")

    assert catalog.stale is False
    assert (
        len(json.dumps(catalog.to_wire(), sort_keys=True).encode("utf-8"))
        == MAX_CATALOG_WIRE_BYTES - 1
    )


def test_host_oversized_refresh_preserves_smaller_last_known_good_catalog():
    adapter = SizedAdapter(
        DiscoveredCatalog(
            account_scope_material="catalog-boundary-scope",
            harness_version="1.0",
            models=(ModelOption(id="last-good", display_name="Last good"),),
        )
    )
    service = ModelCatalogService(
        host_id="mac-mini",
        adapters={"codex": adapter},
        scope_ids=AccountScopeIDs(secret=b"w" * 32),
        clock=lambda: NOW,
    )
    last_good = service.read("codex")
    adapter.discovered = _sized_discovery(MAX_CATALOG_WIRE_BYTES + 1)

    degraded = service.read("codex", force=True)

    assert degraded.stale is True
    assert degraded.stale_reason == "protocol_error"
    assert degraded.models == last_good.models


def test_host_first_oversized_catalog_is_exact_empty_protocol_error():
    service = ModelCatalogService(
        host_id="mac-mini",
        adapters={"codex": SizedAdapter(_sized_discovery(MAX_CATALOG_WIRE_BYTES + 1))},
        scope_ids=AccountScopeIDs(secret=b"w" * 32),
        clock=lambda: NOW,
    )

    catalog = service.read("codex")

    assert catalog == CatalogEnvelope.empty_failure(
        "mac-mini", "codex", "protocol_error"
    )


def test_opaque_model_and_effort_ids_round_trip_without_trimming():
    raw_model = " model-with-space "
    raw_effort = " effort-with-space "
    adapter = SizedAdapter(
        DiscoveredCatalog(
            account_scope_material="opaque-scope",
            harness_version="1.0",
            models=(
                ModelOption(
                    id=raw_model,
                    display_name="Model with intentional spaces",
                    reasoning=ReasoningOptions(
                        supported=(raw_effort,), default=raw_effort
                    ),
                ),
            ),
        )
    )
    service = ModelCatalogService(
        host_id="mac-mini",
        adapters={"codex": adapter},
        scope_ids=AccountScopeIDs(secret=b"o" * 32),
        clock=lambda: NOW,
    )

    wire = service.read("codex").to_wire()
    parsed = CatalogEnvelope.from_wire(wire, "mac-mini", "codex")
    service.validate("codex", raw_model, raw_effort)

    assert parsed.models[0].id == raw_model
    assert parsed.models[0].reasoning is not None
    assert parsed.models[0].reasoning.supported == (raw_effort,)
    with pytest.raises(CatalogSelectionError, match="no longer available"):
        service.validate("codex", raw_model.strip(), raw_effort)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: ModelOption(id=" \t ", display_name="Model"),
        lambda: ReasoningOptions(supported=(" \n ",)),
    ],
)
def test_identifier_validation_rejects_whitespace_only_values(factory):
    with pytest.raises(ValueError):
        factory()
