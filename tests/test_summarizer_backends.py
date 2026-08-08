"""Tests for the LLM backend abstraction (Anthropic + Ollama + selector)."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
import requests

from drover.server.summarizer.backends import (
    BackendError,
    SummarizerBackendConfig,
    select_backend,
)
from drover.server.summarizer.backends.anthropic import AnthropicBackend
from drover.server.summarizer.backends.ollama import OllamaBackend
from drover.server.wol import GpuRig


@pytest.fixture(autouse=True)
def _isolate_oauth_sources(monkeypatch, tmp_path):
    """Stop tests from accidentally picking up the developer's real
    ``~/.claude/.credentials.json`` or env-injected OAuth token. Tests
    that want a token must opt in by overriding these explicitly.
    """
    monkeypatch.setenv(
        "DROVER_CLAUDE_CREDENTIALS_PATH", str(tmp_path / "no-such-creds.json")
    )
    monkeypatch.delenv("ANTHROPIC_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("DROVER_LOCAL_OLLAMA_URL", raising=False)
    monkeypatch.delenv("NEXUS_LOCAL_OLLAMA_URL", raising=False)
    monkeypatch.delenv("DROVER_SUMMARIZER_BACKEND_POLICY", raising=False)
    monkeypatch.delenv("NEXUS_SUMMARIZER_BACKEND_POLICY", raising=False)
    monkeypatch.delenv("DROVER_SUMMARIZER_OLLAMA_KEEP_ALIVE", raising=False)
    monkeypatch.delenv("NEXUS_SUMMARIZER_OLLAMA_KEEP_ALIVE", raising=False)


# --- AnthropicBackend ---------------------------------------------------------


class _FakeBlock:
    def __init__(self, text):
        self.text = text


class _FakeMsg:
    def __init__(self, text):
        self.content = [_FakeBlock(text)]


class _FakeMessages:
    def __init__(self, text):
        self._text = text

    def create(self, **_):
        return _FakeMsg(self._text)


class _FakeAnthropicClient:
    def __init__(self, text):
        self.messages = _FakeMessages(text)


def test_anthropic_backend_returns_parsed_dict() -> None:
    fake = _FakeAnthropicClient(
        json.dumps(
            {
                "summary_md": "did the thing",
                "next_steps_md": "next!",
                "open_questions": [],
            }
        )
    )
    backend = AnthropicBackend(api_key="sk-test", _client=fake)
    out = backend.summarize("prompt")
    assert out["summary_md"] == "did the thing"


def test_anthropic_backend_requires_api_key() -> None:
    with pytest.raises(BackendError, match="api_key"):
        AnthropicBackend(api_key="")


def test_anthropic_backend_translates_client_error() -> None:
    fake = _FakeAnthropicClient("not json")
    backend = AnthropicBackend(api_key="sk-test", _client=fake)
    with pytest.raises(BackendError, match="anthropic"):
        backend.summarize("p")


# --- OllamaBackend ------------------------------------------------------------


class _FakeResp:
    def __init__(self, *, status=200, payload=None, text="", ok=True):
        self.status_code = status
        self.text = text
        self.ok = ok
        self._payload = payload

    def json(self):
        if self._payload is None:
            return {"models": [{"name": "qwen3.5:35b-a3b"}]}
        return self._payload


def _rig() -> GpuRig:
    return GpuRig(
        relay_url="http://relay:9753", ollama_url="http://gpu:11434", wake_timeout_s=5
    )


def test_ollama_backend_summarize_happy_path() -> None:
    summary_payload = {
        "summary_md": "x",
        "next_steps_md": "y",
        "open_questions": ["q1"],
    }
    envelope = {"response": json.dumps(summary_payload), "done": True}

    def _get(url, *a, **kw):
        # health check during ensure_ready
        return _FakeResp()

    with (
        patch("drover.server.wol.requests.get", side_effect=_get),
        patch(
            "drover.server.summarizer.backends.ollama.requests.post",
            return_value=_FakeResp(payload=envelope),
        ),
    ):
        b = OllamaBackend(rig=_rig(), model="qwen3.5:35b-a3b")
        out = b.summarize("prompt body")
    assert out["summary_md"] == "x"
    assert out["open_questions"] == ["q1"]


def test_ollama_backend_uses_short_default_keep_alive() -> None:
    envelope = {
        "response": json.dumps(
            {"summary_md": "x", "next_steps_md": "y", "open_questions": []}
        )
    }
    with (
        patch("drover.server.wol.requests.get", return_value=_FakeResp()),
        patch(
            "drover.server.summarizer.backends.ollama.requests.post",
            return_value=_FakeResp(payload=envelope),
        ) as mock_post,
    ):
        b = OllamaBackend(rig=_rig(), model="qwen3.5:35b-a3b")
        b.summarize("prompt")
    assert mock_post.call_args.kwargs["json"]["keep_alive"] == "30s"


def test_ollama_backend_keep_alive_can_be_overridden(monkeypatch) -> None:
    monkeypatch.setenv("DROVER_SUMMARIZER_OLLAMA_KEEP_ALIVE", "0")
    envelope = {
        "response": json.dumps(
            {"summary_md": "x", "next_steps_md": "y", "open_questions": []}
        )
    }
    with (
        patch("drover.server.wol.requests.get", return_value=_FakeResp()),
        patch(
            "drover.server.summarizer.backends.ollama.requests.post",
            return_value=_FakeResp(payload=envelope),
        ) as mock_post,
    ):
        b = OllamaBackend(rig=_rig(), model="qwen3.5:35b-a3b")
        b.summarize("prompt")
    assert mock_post.call_args.kwargs["json"]["keep_alive"] == "0"


def test_ollama_backend_strips_markdown_fence() -> None:
    fenced = (
        "```json\n"
        + json.dumps(
            {
                "summary_md": "z",
                "next_steps_md": "w",
                "open_questions": [],
            }
        )
        + "\n```"
    )
    envelope = {"response": fenced}
    with (
        patch("drover.server.wol.requests.get", return_value=_FakeResp()),
        patch(
            "drover.server.summarizer.backends.ollama.requests.post",
            return_value=_FakeResp(payload=envelope),
        ),
    ):
        b = OllamaBackend(rig=_rig(), model="qwen3.5:35b-a3b")
        out = b.summarize("prompt")
    assert out["summary_md"] == "z"


def test_ollama_backend_raises_on_missing_keys() -> None:
    envelope = {"response": json.dumps({"summary_md": "only this"})}
    with (
        patch("drover.server.wol.requests.get", return_value=_FakeResp()),
        patch(
            "drover.server.summarizer.backends.ollama.requests.post",
            return_value=_FakeResp(payload=envelope),
        ),
    ):
        b = OllamaBackend(rig=_rig(), model="qwen3.5:35b-a3b")
        with pytest.raises(BackendError, match="missing required keys"):
            b.summarize("prompt")


def test_ollama_backend_raises_on_http_error() -> None:
    with (
        patch("drover.server.wol.requests.get", return_value=_FakeResp()),
        patch(
            "drover.server.summarizer.backends.ollama.requests.post",
            side_effect=requests.ConnectionError("dropped"),
        ),
    ):
        b = OllamaBackend(rig=_rig(), model="qwen3.5:35b-a3b")
        with pytest.raises(BackendError, match="HTTP error"):
            b.summarize("prompt")


def test_ollama_backend_skips_wake_if_disabled() -> None:
    """wake_on_first_call=False → no health check fired before the request."""
    with (
        patch("drover.server.wol.requests.get") as mock_get,
        patch(
            "drover.server.summarizer.backends.ollama.requests.post",
            return_value=_FakeResp(
                payload={
                    "response": json.dumps(
                        {
                            "summary_md": "s",
                            "next_steps_md": "n",
                            "open_questions": [],
                        }
                    )
                }
            ),
        ),
    ):
        b = OllamaBackend(rig=_rig(), model="qwen3.5:35b-a3b", wake_on_first_call=False)
        b.summarize("prompt")
    mock_get.assert_not_called()


def test_ollama_backend_kickstarts_launchd_before_request() -> None:
    with (
        patch("drover.server.summarizer.backends.ollama.subprocess.run") as mock_run,
        patch(
            "drover.server.wol.requests.get",
            return_value=_FakeResp(payload={"models": [{"name": "qwen3.5:35b-a3b"}]}),
        ) as mock_get,
        patch(
            "drover.server.summarizer.backends.ollama.requests.post",
            return_value=_FakeResp(
                payload={
                    "response": json.dumps(
                        {
                            "summary_md": "s",
                            "next_steps_md": "n",
                            "open_questions": [],
                        }
                    )
                }
            ),
        ) as mock_post,
    ):
        b = OllamaBackend(
            rig=GpuRig(relay_url="", ollama_url="http://127.0.0.1:11435"),
            model="qwen3.5:35b-a3b",
            launchd_label="com.nexus.mac-ollama-embeddings",
        )
        b.summarize("prompt")

    assert mock_run.call_args.args[0][:3] == [
        "/bin/launchctl",
        "kickstart",
        f"gui/{__import__('os').getuid()}/com.nexus.mac-ollama-embeddings",
    ]
    mock_get.assert_called_once_with("http://127.0.0.1:11435/api/tags", timeout=3.0)
    assert mock_post.call_args.args[0] == "http://127.0.0.1:11435/api/generate"


# --- select_backend -----------------------------------------------------------


def test_select_backend_incremental_prefers_api_when_anthropic_available() -> None:
    cfg = SummarizerBackendConfig(api_key="sk-test", gpu_rig=_rig())
    b = select_backend(job_kind="incremental", config=cfg)
    assert b.name == "hybrid"
    assert b.model == cfg.api_model


def test_select_backend_backfill_prefers_api() -> None:
    cfg = SummarizerBackendConfig(api_key="sk-test", gpu_rig=_rig())
    b = select_backend(job_kind="backfill", config=cfg)
    assert b.name == "hybrid"
    assert b.model == cfg.api_model


def test_select_backend_falls_back_to_api_when_no_rig() -> None:
    cfg = SummarizerBackendConfig(api_key="sk-test", gpu_rig=None)
    b = select_backend(job_kind="incremental", config=cfg)
    assert b.name == "anthropic"


def test_select_backend_falls_back_to_local_when_no_api() -> None:
    cfg = SummarizerBackendConfig(api_key=None, gpu_rig=_rig())
    b = select_backend(job_kind="backfill", config=cfg)
    assert b.name == "ollama"


def test_select_backend_hybrid_falls_back_to_local_on_anthropic_401() -> None:
    cfg = SummarizerBackendConfig(api_key="sk-test", gpu_rig=_rig())
    b = select_backend(job_kind="incremental", config=cfg)

    with (
        patch.object(
            b.primary,
            "summarize",
            side_effect=BackendError(
                "anthropic: Error code: 401 - invalid authentication credentials"
            ),
        ) as primary_summarize,
        patch.object(
            b.fallback,
            "summarize",
            return_value={
                "summary_md": "local summary",
                "next_steps_md": "local next",
                "open_questions": [],
            },
        ) as fallback_summarize,
    ):
        out = b.summarize("prompt")

    assert out["summary_md"] == "local summary"
    assert b.model == b.fallback.model
    primary_summarize.assert_called_once_with("prompt")
    fallback_summarize.assert_called_once_with("prompt")


def test_select_backend_hybrid_does_not_fallback_on_non_auth_anthropic_error() -> None:
    cfg = SummarizerBackendConfig(api_key="sk-test", gpu_rig=_rig())
    b = select_backend(job_kind="incremental", config=cfg)

    with (
        patch.object(
            b.primary,
            "summarize",
            side_effect=BackendError("anthropic: LLM response missing required keys"),
        ),
        patch.object(b.fallback, "summarize") as fallback_summarize,
        pytest.raises(BackendError, match="missing required keys"),
    ):
        b.summarize("prompt")

    fallback_summarize.assert_not_called()


def test_select_backend_cloud_policy_uses_anthropic_without_runtime_fallback() -> None:
    cfg = SummarizerBackendConfig(
        backend_policy="cloud",
        api_key="sk-test",
        gpu_rig=_rig(),
    )
    b = select_backend(job_kind="incremental", config=cfg)
    assert b.name == "anthropic"


def test_select_backend_cloud_policy_refuses_local_fallback() -> None:
    cfg = SummarizerBackendConfig(
        backend_policy="cloud",
        api_key=None,
        gpu_rig=_rig(),
    )
    with pytest.raises(BackendError, match="backend_policy=cloud"):
        select_backend(job_kind="incremental", config=cfg)


def test_select_backend_local_policy_ignores_api_credentials() -> None:
    cfg = SummarizerBackendConfig(
        backend_policy="local",
        api_key="sk-test",
        gpu_rig=_rig(),
    )
    b = select_backend(job_kind="incremental", config=cfg)
    assert b.name == "ollama"


def test_select_backend_local_policy_requires_local_backend() -> None:
    cfg = SummarizerBackendConfig(
        backend_policy="local",
        api_key="sk-test",
        gpu_rig=None,
    )
    with pytest.raises(BackendError, match="backend_policy=local"):
        select_backend(job_kind="incremental", config=cfg)


def test_select_backend_local_ollama_kickstarts_launchd_when_no_api() -> None:
    cfg = SummarizerBackendConfig.from_runtime(
        local_ollama_url="http://127.0.0.1:11435"
    )
    b = select_backend(job_kind="backfill", config=cfg)
    assert b.name == "ollama"
    assert isinstance(b, OllamaBackend)
    assert b.wake_on_first_call is True
    assert b.launchd_label == "com.drover.mac-ollama-embeddings"


def test_select_backend_raises_when_neither_configured() -> None:
    cfg = SummarizerBackendConfig(api_key=None, gpu_rig=None)
    with pytest.raises(BackendError, match="no backend configured"):
        select_backend(job_kind="incremental", config=cfg)


# --- SummarizerBackendConfig.from_runtime ------------------------------------


def test_from_runtime_builds_rig_when_both_urls_present() -> None:
    cfg = SummarizerBackendConfig.from_runtime(
        api_key="sk-x",
        gpu_relay_url="http://relay:9753",
        gpu_ollama_url="http://gpu:11434",
    )
    assert cfg.gpu_rig is not None
    assert cfg.gpu_rig.relay_url == "http://relay:9753"
    assert cfg.gpu_rig.ollama_url == "http://gpu:11434"
    assert cfg.wake_on_first_call is True


def test_from_runtime_builds_local_ollama_with_launchd_wake() -> None:
    cfg = SummarizerBackendConfig.from_runtime(
        local_ollama_url="http://127.0.0.1:11435"
    )
    assert cfg.gpu_rig is not None
    assert cfg.gpu_rig.relay_url == ""
    assert cfg.gpu_rig.ollama_url == "http://127.0.0.1:11435"
    assert cfg.wake_on_first_call is True
    assert cfg.local_ollama_launchd_label == "com.drover.mac-ollama-embeddings"
    assert (
        cfg.local_ollama_launchd_plist
        == "~/Library/LaunchAgents/com.drover.mac-ollama-embeddings.plist"
    )


def test_from_runtime_no_rig_when_only_one_url(monkeypatch) -> None:
    monkeypatch.delenv("DROVER_GPU_RELAY_URL", raising=False)
    monkeypatch.delenv("NEXUS_GPU_RELAY_URL", raising=False)
    monkeypatch.delenv("DROVER_GPU_OLLAMA_URL", raising=False)
    monkeypatch.delenv("NEXUS_GPU_OLLAMA_URL", raising=False)
    cfg = SummarizerBackendConfig.from_runtime(
        api_key="sk-x",
        gpu_relay_url="http://relay:9753",
    )
    assert cfg.gpu_rig is None


def test_from_runtime_reads_backend_policy_from_arg() -> None:
    cfg = SummarizerBackendConfig.from_runtime(
        backend_policy="cloud",
        local_ollama_url="http://127.0.0.1:11435",
    )
    assert cfg.backend_policy == "cloud"
    assert cfg.allows_anthropic is True
    assert cfg.allows_local_backend is False


def test_from_runtime_reads_backend_policy_from_env(monkeypatch) -> None:
    monkeypatch.setenv("DROVER_SUMMARIZER_BACKEND_POLICY", "local")
    cfg = SummarizerBackendConfig.from_runtime(api_key="sk-test")
    assert cfg.backend_policy == "local"
    assert cfg.allows_anthropic is False
    assert cfg.allows_local_backend is True


def test_from_runtime_rejects_unknown_backend_policy() -> None:
    with pytest.raises(ValueError, match="backend_policy"):
        SummarizerBackendConfig.from_runtime(backend_policy="surprise")


# --- credentials.json auto-refresh -------------------------------------------


def _write_creds(path, token: str, *, expires_at_ms: int | None = None) -> None:
    import time as _t

    payload = {
        "claudeAiOauth": {
            "accessToken": token,
            "refreshToken": "rt",
            "expiresAt": (
                expires_at_ms
                if expires_at_ms is not None
                else int((_t.time() + 3600) * 1000)
            ),
            "scopes": ["user:inference"],
        }
    }
    path.write_text(json.dumps(payload))


def test_default_claude_credentials_path_matches_claude_code_location() -> None:
    from drover.server.summarizer.backends.config import DEFAULT_CLAUDE_CREDENTIALS_PATH

    assert DEFAULT_CLAUDE_CREDENTIALS_PATH == "~/.claude/.credentials.json"


def test_effective_auth_token_reads_credentials_file(tmp_path, monkeypatch) -> None:
    creds = tmp_path / ".credentials.json"
    _write_creds(creds, "tok-from-file")
    monkeypatch.setenv("DROVER_CLAUDE_CREDENTIALS_PATH", str(creds))
    monkeypatch.delenv("ANTHROPIC_OAUTH_TOKEN", raising=False)

    cfg = SummarizerBackendConfig(api_key=None, auth_token=None)
    assert cfg.effective_auth_token() == "tok-from-file"
    assert cfg.has_anthropic_creds is True


def test_effective_auth_token_picks_up_rotation(tmp_path, monkeypatch) -> None:
    creds = tmp_path / ".credentials.json"
    _write_creds(creds, "tok-v1")
    monkeypatch.setenv("DROVER_CLAUDE_CREDENTIALS_PATH", str(creds))
    monkeypatch.delenv("ANTHROPIC_OAUTH_TOKEN", raising=False)

    cfg = SummarizerBackendConfig(api_key=None, auth_token="frozen-old")
    assert cfg.effective_auth_token() == "tok-v1"

    _write_creds(creds, "tok-v2")
    assert cfg.effective_auth_token() == "tok-v2"


def test_effective_auth_token_file_wins_over_env(tmp_path, monkeypatch) -> None:
    creds = tmp_path / ".credentials.json"
    _write_creds(creds, "tok-from-file")
    monkeypatch.setenv("DROVER_CLAUDE_CREDENTIALS_PATH", str(creds))
    monkeypatch.setenv("ANTHROPIC_OAUTH_TOKEN", "tok-from-env")

    cfg = SummarizerBackendConfig(api_key=None, auth_token=None)
    assert cfg.effective_auth_token() == "tok-from-file"


def test_effective_auth_token_falls_back_to_env_when_file_missing(
    tmp_path, monkeypatch
) -> None:
    missing = tmp_path / "nope.json"
    monkeypatch.setenv("DROVER_CLAUDE_CREDENTIALS_PATH", str(missing))
    monkeypatch.setenv("ANTHROPIC_OAUTH_TOKEN", "tok-from-env")

    cfg = SummarizerBackendConfig(api_key=None, auth_token=None)
    assert cfg.effective_auth_token() == "tok-from-env"


def test_effective_auth_token_skips_expired_file_token(tmp_path, monkeypatch) -> None:
    creds = tmp_path / ".credentials.json"
    _write_creds(creds, "tok-stale", expires_at_ms=1)  # epoch start → expired
    monkeypatch.setenv("DROVER_CLAUDE_CREDENTIALS_PATH", str(creds))
    monkeypatch.setenv("ANTHROPIC_OAUTH_TOKEN", "tok-from-env")

    cfg = SummarizerBackendConfig(api_key=None, auth_token=None)
    assert cfg.effective_auth_token() == "tok-from-env"


def test_effective_auth_token_skipped_when_api_key_set(tmp_path, monkeypatch) -> None:
    creds = tmp_path / ".credentials.json"
    _write_creds(creds, "tok-from-file")
    monkeypatch.setenv("DROVER_CLAUDE_CREDENTIALS_PATH", str(creds))

    cfg = SummarizerBackendConfig(api_key="sk-x", auth_token=None)
    assert cfg.effective_auth_token() is None
    assert cfg.has_anthropic_creds is True


def test_from_runtime_defaults_launchd_label_to_drover_service(monkeypatch) -> None:
    for var in (
        "DROVER_LOCAL_OLLAMA_LAUNCHD_LABEL",
        "NEXUS_LOCAL_OLLAMA_LAUNCHD_LABEL",
        "DROVER_LOCAL_OLLAMA_LAUNCHD_PLIST",
        "NEXUS_LOCAL_OLLAMA_LAUNCHD_PLIST",
    ):
        monkeypatch.delenv(var, raising=False)

    cfg = SummarizerBackendConfig.from_runtime(
        local_ollama_url="http://127.0.0.1:11435"
    )
    assert cfg.local_ollama_launchd_label == "com.drover.mac-ollama-embeddings"
    assert cfg.local_ollama_launchd_plist is not None
    assert cfg.local_ollama_launchd_plist.endswith(
        "com.drover.mac-ollama-embeddings.plist"
    )


def test_from_runtime_explicit_launchd_overrides_win(monkeypatch) -> None:
    for var in (
        "DROVER_LOCAL_OLLAMA_LAUNCHD_LABEL",
        "NEXUS_LOCAL_OLLAMA_LAUNCHD_LABEL",
        "DROVER_LOCAL_OLLAMA_LAUNCHD_PLIST",
        "NEXUS_LOCAL_OLLAMA_LAUNCHD_PLIST",
    ):
        monkeypatch.delenv(var, raising=False)

    cfg = SummarizerBackendConfig.from_runtime(
        local_ollama_url="http://127.0.0.1:11435",
        local_ollama_launchd_label="com.custom.ollama",
        local_ollama_launchd_plist="/tmp/com.custom.ollama.plist",
    )
    assert cfg.local_ollama_launchd_label == "com.custom.ollama"
    assert cfg.local_ollama_launchd_plist == "/tmp/com.custom.ollama.plist"
