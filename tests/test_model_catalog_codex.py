import json
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from drover.server.harness.model_catalog import AccountScopeIDs, ModelCatalogService
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
