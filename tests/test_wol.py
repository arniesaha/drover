"""Tests for the WoL helper — relay wake + Ollama health polling."""

from __future__ import annotations

from unittest.mock import patch

import pytest
import requests

from drover.server.wol import (
    GpuRig,
    GpuWakeError,
    ensure_gpu_awake,
    wait_for_ollama,
    wake_via_relay,
)


class _FakeResp:
    def __init__(self, status_code=200, text="ok", ok=True, payload=None):
        self.status_code = status_code
        self.text = text
        self.ok = ok
        self._payload = payload

    def json(self):
        if self._payload is None:
            return {"models": []}
        return self._payload


def test_wake_via_relay_success() -> None:
    with patch("drover.server.wol.requests.get", return_value=_FakeResp()) as mock_get:
        wake_via_relay("http://relay.example:9753")
    mock_get.assert_called_once_with("http://relay.example:9753/wake", timeout=5.0)


def test_wake_via_relay_unreachable_raises() -> None:
    with patch(
        "drover.server.wol.requests.get", side_effect=requests.ConnectionError("boom")
    ):
        with pytest.raises(GpuWakeError, match="unreachable"):
            wake_via_relay("http://relay.example:9753")


def test_wake_via_relay_non_2xx_raises() -> None:
    bad = _FakeResp(status_code=500, text="oh no", ok=False)
    with patch("drover.server.wol.requests.get", return_value=bad):
        with pytest.raises(GpuWakeError, match="500"):
            wake_via_relay("http://relay.example:9753")


def test_wait_for_ollama_returns_when_healthy() -> None:
    with patch("drover.server.wol.requests.get", return_value=_FakeResp()):
        wait_for_ollama("http://gpu:11434", timeout_s=10.0, sleep=lambda s: None)


def test_wait_for_ollama_waits_for_model() -> None:
    responses = [
        _FakeResp(payload={"models": [{"name": "other"}]}),
        _FakeResp(payload={"models": [{"name": "nomic-embed-text"}]}),
    ]
    sleeps: list[float] = []

    with patch("drover.server.wol.requests.get", side_effect=responses):
        wait_for_ollama(
            "http://gpu:11434",
            timeout_s=10.0,
            model="nomic-embed-text",
            poll_interval_s=0.25,
            sleep=lambda s: sleeps.append(s),
        )

    assert sleeps == [0.25]


def test_wait_for_ollama_accepts_latest_model_tag() -> None:
    with patch(
        "drover.server.wol.requests.get",
        return_value=_FakeResp(
            payload={"models": [{"name": "nomic-embed-text:latest"}]}
        ),
    ):
        wait_for_ollama(
            "http://gpu:11434",
            timeout_s=10.0,
            model="nomic-embed-text",
            sleep=lambda s: None,
        )


def test_wait_for_ollama_model_timeout_mentions_model() -> None:
    with patch(
        "drover.server.wol.requests.get",
        return_value=_FakeResp(payload={"models": [{"name": "other"}]}),
    ):
        with pytest.raises(GpuWakeError, match="missing-model"):
            wait_for_ollama(
                "http://gpu:11434",
                timeout_s=0.05,
                model="missing-model",
                poll_interval_s=0.01,
                sleep=lambda s: None,
            )


def test_wait_for_ollama_polls_then_succeeds() -> None:
    responses = [
        requests.ConnectionError("not yet"),
        requests.ConnectionError("still not"),
        _FakeResp(),
    ]
    sleeps: list[float] = []

    def _get(*a, **kw):
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    with patch("drover.server.wol.requests.get", side_effect=_get):
        wait_for_ollama(
            "http://gpu:11434",
            timeout_s=30.0,
            poll_interval_s=0.01,
            sleep=lambda s: sleeps.append(s),
        )
    assert sleeps == [0.01, 0.01]


def test_wait_for_ollama_times_out() -> None:
    with patch(
        "drover.server.wol.requests.get",
        side_effect=requests.ConnectionError("never up"),
    ):
        with pytest.raises(GpuWakeError, match="did not become reachable"):
            wait_for_ollama(
                "http://gpu:11434",
                timeout_s=0.05,
                poll_interval_s=0.01,
                sleep=lambda s: None,
            )


def test_ensure_gpu_awake_noop_when_healthy() -> None:
    rig = GpuRig(relay_url="http://relay:9753", ollama_url="http://gpu:11434")
    with patch("drover.server.wol.requests.get", return_value=_FakeResp()) as mock_get:
        woke = ensure_gpu_awake(rig)
    assert woke is False
    # Only the health check, no wake call
    assert mock_get.call_count == 1
    assert mock_get.call_args.args[0].endswith("/api/tags")


def test_ensure_gpu_awake_wakes_when_cold() -> None:
    """First /api/tags fails → wake POST → then /api/tags succeeds."""
    calls: list[str] = []

    def _get(url, *args, **kwargs):
        calls.append(url)
        if url.endswith("/api/tags"):
            if calls.count(url) == 1:
                raise requests.ConnectionError("cold")
            return _FakeResp()
        if url.endswith("/wake"):
            return _FakeResp()
        raise AssertionError(f"unexpected url {url}")

    rig = GpuRig(
        relay_url="http://relay:9753",
        ollama_url="http://gpu:11434",
        wake_timeout_s=5.0,
        poll_interval_s=0.01,
    )
    with patch("drover.server.wol.requests.get", side_effect=_get):
        woke = ensure_gpu_awake(rig, sleep=lambda s: None)
    assert woke is True
    # First check failed, wake fired, second check succeeded
    assert calls == [
        "http://gpu:11434/api/tags",
        "http://relay:9753/wake",
        "http://gpu:11434/api/tags",
    ]
