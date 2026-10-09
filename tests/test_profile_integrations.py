"""Verify the copyable client hook without installing client configuration."""

import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def hook(monkeypatch):
    path = Path(__file__).parents[1] / "docs/integrations/claude-session-start.py"
    spec = importlib.util.spec_from_file_location("profile_startup_example", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("DROVER_MCP_URL", "https://example.invalid/mcp")
    return module


def test_claude_hook_calls_once_and_injects_bundle(hook, monkeypatch, capsys):
    calls = []
    profile = {
        "bundle": "# Drover profile\n[user/rule] Use tables.\nWithheld: 1 items.\n",
        "data_watermark": {"timestamp": None, "basis": "unknown"},
        "oldest_item_age_seconds": None,
    }

    def call(url, name, arguments, **kwargs):
        calls.append((url, name, arguments, kwargs))
        return {"content": [{"type": "text", "text": json.dumps(profile)}]}

    monkeypatch.setattr(hook, "call_tool", call)
    hook.main()
    output = capsys.readouterr()
    assert not output.err
    assert calls == [
        (
            "https://example.invalid/mcp",
            "drover_profile",
            {"scope": "first_turn"},
            {"timeout": 5},
        )
    ]
    value = json.loads(output.out)["hookSpecificOutput"]
    assert value["hookEventName"] == "SessionStart"
    context = json.loads(value["additionalContext"])
    assert context["profile_context"] == profile["bundle"]
    assert context["data_watermark"] == profile["data_watermark"]
    assert context["oldest_item_age_seconds"] is None


@pytest.mark.parametrize(
    "response",
    [
        {"isError": True, "content": []},
        {"content": [{"type": "text", "text": '{"status":"timeout"}'}]},
        {"content": [{"type": "text", "text": '{"status":"busy"}'}]},
        {"content": [{"type": "text", "text": "Malformed synthetic body"}]},
    ],
)
def test_claude_hook_failure_never_injects_remote_content(
    hook, monkeypatch, capsys, response
):
    monkeypatch.setattr(hook, "call_tool", lambda *a, **kw: response)
    hook.main()
    output = capsys.readouterr()
    assert not output.out
    assert output.err == "Drover profile unavailable for this session.\n"


def test_claude_hook_connection_failure(hook, monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise OSError("Synthetic connection failure")

    monkeypatch.setattr(hook, "call_tool", fail)
    hook.main()
    output = capsys.readouterr()
    assert not output.out
    assert "Synthetic" not in output.err
