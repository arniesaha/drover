"""Model selection wire contracts backed by the fake Codex app-server."""

from __future__ import annotations

from .test_harnessd_contract import request


def test_model_catalog_advertises_selectable_codex_preferences(daemon):
    status, catalog = request(daemon, "/model-catalog?harness=codex&refresh=1")
    assert status == 200
    model = next(item for item in catalog["models"] if item["id"] == "contract-model")
    assert model["is_default"] is True
    assert model["reasoning"] == {"supported": ["low", "high"], "default": "low"}


def test_model_preference_validation_passes_codex_launch_preferences(daemon):
    status, created = request(
        daemon,
        "/sessions",
        payload={
            "harness": "codex",
            "mode": "structured",
            "cwd": str(daemon.root),
            "model": "contract-model",
            "thinking_effort": "high",
        },
    )
    assert status == 201
    assert (created["model"], created["thinking_effort"]) == ("contract-model", "high")


def test_model_preference_validation_passes_codex_turn_preferences_unchanged(daemon):
    status, created = request(
        daemon,
        "/sessions",
        payload={"harness": "codex", "mode": "structured", "cwd": str(daemon.root)},
    )
    assert status == 201
    status, turn = request(
        daemon,
        f"/sessions/{created['session_id']}/turns",
        payload={
            "text": "continue",
            "model": "contract-model",
            "thinking_effort": "high",
        },
    )
    assert status == 202
    assert isinstance(turn["turn_id"], str)
