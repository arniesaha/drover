from __future__ import annotations

from drover.server.harness.model_catalog.deepseek import DeepSeekCatalogAdapter


class FakeApi:
    base_url = "http://127.0.0.1:3080"

    def call(self, method: str, payload: dict) -> dict:
        assert method == "llm.models"
        assert payload == {}
        return {
            "groups": [
                {
                    "id": "deepseek-official",
                    "name": "DeepSeek",
                    "models": [
                        {
                            "id": "deepseek-v4-pro",
                            "name": "DeepSeek V4 Pro",
                            "reasoning": {
                                "efforts": [
                                    {"id": "off", "name": "Off"},
                                    {"id": "high", "name": "High"},
                                ],
                                "defaultEffort": "high",
                            },
                        }
                    ],
                },
                {
                    "id": "ollama",
                    "name": "Ollama",
                    "models": [{"id": "qwen3.5:35b-a3b", "name": "Qwen 3.5"}],
                },
            ],
            "failures": [],
        }


def test_catalog_preserves_provider_identity_and_reasoning(monkeypatch) -> None:
    monkeypatch.setattr(
        "drover.server.harness.model_catalog.deepseek.version", lambda command: "0.1.0"
    )
    catalog = DeepSeekCatalogAdapter(["/opt/dsh"], api=FakeApi()).discover()
    assert catalog.harness_version == "0.1.0"
    assert [model.id for model in catalog.models] == [
        "deepseek-official/deepseek-v4-pro",
        "ollama/qwen3.5:35b-a3b",
    ]
    assert catalog.models[0].reasoning.supported == ("off", "high")
    assert catalog.models[0].reasoning.default == "high"
    assert catalog.models[1].is_default is True
