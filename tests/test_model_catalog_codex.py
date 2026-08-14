import json
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from drover.server.harness.model_catalog import (
    AccountScopeIDs,
    CatalogDiscoveryError,
    ModelCatalogService,
)
from drover.server.harness.model_catalog.codex import CodexCatalogAdapter


@pytest.fixture
def fake_codex_executable(tmp_path):
    executable = tmp_path / "codex"
    executable.write_text("""#!/usr/bin/env python3
import json
import sys

if "--version" in sys.argv:
    print("codex-cli 0.147.0")
    raise SystemExit(0)

if "fail" in sys.argv:
    raise SystemExit(1)

for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    if request["method"] == "initialize":
        result = {"userAgent": "fake"}
    elif request["method"] == "account/read":
        result = {
            "account": {
                "type": "chatgpt",
                "email": "person@example.com",
                "planType": "plus",
            },
            "requiresOpenaiAuth": True,
        }
    elif request["method"] == "model/list":
        if request["params"]["cursor"] is None:
            result = {
                "data": [
                    {
                        "model": "gpt-5.6-sol",
                        "displayName": "GPT-5.6 Sol",
                        "description": "Frontier",
                        "isDefault": True,
                        "defaultReasoningEffort": "medium",
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": "low"},
                            {"reasoningEffort": "medium"},
                            {"reasoningEffort": "ultra"},
                        ],
                    },
                    {
                        "model": "hidden-model",
                        "displayName": "Hidden model",
                        "isHidden": True,
                    },
                ],
                "nextCursor": "second-page",
            }
        else:
            result = {
                "data": [
                    {
                        "model": "gpt-5.6-terra",
                        "displayName": "GPT-5.6 Terra",
                        "description": "Balanced",
                        "defaultReasoningEffort": "medium",
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": "low"},
                            {"reasoningEffort": "medium"},
                            {"reasoningEffort": "high"},
                            {"reasoningEffort": "xhigh"},
                            {"reasoningEffort": "max"},
                            {"reasoningEffort": "ultra"},
                        ],
                    },
                    {
                        "id": "gpt-5.6-luna",
                        "displayName": "GPT-5.6 Luna",
                        "description": "Fast",
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": "low"},
                            {"reasoningEffort": "medium"},
                            {"reasoningEffort": "max"},
                        ],
                        "defaultReasoningEffort": "medium",
                    },
                ],
                "nextCursor": None,
            }
    else:
        continue
    print(json.dumps({"id": request["id"], "result": result}), flush=True)
""")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable


def test_discovers_paginated_live_codex_models_without_account_data(
    fake_codex_executable, tmp_path
):
    discovered = CodexCatalogAdapter(
        (str(fake_codex_executable), "app-server", "--stdio"), codex_home=tmp_path
    ).discover()

    assert [model.id for model in discovered.models] == [
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
    ]
    terra = discovered.models[1]
    luna = discovered.models[2]
    assert terra.reasoning is not None
    assert luna.reasoning is not None
    assert terra.reasoning.default == "medium"
    assert terra.reasoning.supported[-1] == "ultra"
    assert luna.reasoning.supported[-1] == "max"
    assert discovered.harness_version == "codex-cli 0.147.0"
    assert "person@example.com" in discovered.account_scope_material
    assert "plus" in discovered.account_scope_material
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


def test_uses_native_cache_as_an_offline_stale_catalog(fake_codex_executable, tmp_path):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "models_cache.json").write_text(
        json.dumps(
            {
                "client_version": "0.147.0",
                "models": [
                    {
                        "slug": "gpt-5.6-terra",
                        "display_name": "GPT-5.6 Terra",
                        "description": "Balanced",
                        "visibility": "list",
                        "default_reasoning_level": "medium",
                        "supported_reasoning_levels": [
                            {"effort": "low"},
                            {"effort": "medium"},
                            {"effort": "ultra"},
                        ],
                    },
                    {
                        "slug": "hidden-model",
                        "display_name": "Hidden model",
                        "visibility": "hidden",
                    },
                ],
            }
        )
    )
    adapter = CodexCatalogAdapter(
        (str(fake_codex_executable), "fail"), codex_home=codex_home
    )
    service = ModelCatalogService(
        host_id="mac-mini",
        adapters={"codex": adapter},
        scope_ids=AccountScopeIDs(secret=b"x" * 32),
        clock=lambda: datetime(2026, 8, 14, tzinfo=timezone.utc),
    )

    catalog = service.read("codex")

    assert catalog.stale is True
    assert catalog.stale_reason == "offline"
    assert catalog.harness_version == "0.147.0"
    assert [model.id for model in catalog.models] == ["gpt-5.6-terra"]


def test_live_scope_changes_when_effective_codex_config_changes(
    fake_codex_executable, tmp_path
):
    (tmp_path / "config.toml").write_text('model = "gpt-5.6-sol"\n')
    adapter = CodexCatalogAdapter(
        (str(fake_codex_executable), "app-server", "--stdio"), codex_home=tmp_path
    )
    service = ModelCatalogService(
        host_id="mac-mini",
        adapters={"codex": adapter},
        scope_ids=AccountScopeIDs(secret=b"c" * 32),
        clock=lambda: datetime(2026, 8, 14, tzinfo=timezone.utc),
    )

    before = service.read("codex")
    (tmp_path / "config.toml").write_text('model = "gpt-5.6-terra"\n')
    after = service.read("codex", force=True)

    assert before.account_scope_id != after.account_scope_id
    assert after.models == before.models


def test_native_cache_rejects_an_unbounded_mostly_hidden_model_array(
    fake_codex_executable, tmp_path
):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    hidden = {
        "slug": "hidden-model",
        "display_name": "Hidden model",
        "visibility": "hidden",
    }
    (codex_home / "models_cache.json").write_text(
        json.dumps(
            {
                "client_version": "0.147.0",
                "models": [hidden] * 257
                + [
                    {
                        "slug": "gpt-5.6-terra",
                        "display_name": "GPT-5.6 Terra",
                        "visibility": "list",
                    }
                ],
            }
        )
    )

    with pytest.raises(CatalogDiscoveryError, match="protocol_error"):
        CodexCatalogAdapter(
            (str(fake_codex_executable), "fail"), codex_home=codex_home
        ).discover()


def test_native_cache_rejects_a_file_larger_than_one_mebibyte(
    fake_codex_executable, tmp_path
):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "models_cache.json").write_text(
        json.dumps(
            {
                "client_version": "0.147.0",
                "padding": "x" * 1_048_577,
                "models": [
                    {
                        "slug": "gpt-5.6-terra",
                        "display_name": "GPT-5.6 Terra",
                        "visibility": "list",
                    }
                ],
            }
        )
    )

    with pytest.raises(CatalogDiscoveryError, match="protocol_error"):
        CodexCatalogAdapter(
            (str(fake_codex_executable), "fail"), codex_home=codex_home
        ).discover()


def test_cache_identity_tracks_a_path_resolved_bare_codex_executable(
    tmp_path, monkeypatch
):
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    executable = binary_dir / "codex"
    executable.write_text("first version")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(binary_dir))
    adapter = CodexCatalogAdapter(("codex",), codex_home=tmp_path)

    before = adapter.cache_identity()
    executable.write_text("replacement version")
    after = adapter.cache_identity()

    assert before != after
