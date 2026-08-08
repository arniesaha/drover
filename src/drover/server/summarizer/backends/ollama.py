"""Ollama backend — calls the GPU rig directly, waking it via WoL if cold.

The first ``summarize`` call after a cold rig pays the wake latency
(typically 30-90s). Subsequent calls within an idle window land
warm. To amortize, the worker batches multiple jobs and drains them
back-to-back so a single wake covers many summaries.

Requests use ``/api/generate`` with ``format="json"`` so Ollama returns
parsed JSON directly. We then validate the same required keys the
Anthropic backend enforces.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from typing import Optional

import requests

from drover.server.summarizer.backends.types import BackendError, BackendReadinessError
from drover.server.wol import GpuRig, GpuWakeError, ensure_gpu_awake, wait_for_ollama

log = logging.getLogger("drover.summarizer.backends.ollama")


_REQUIRED_KEYS = ("summary_md", "next_steps_md", "open_questions")
_OPTIONAL_KEYS = ("last_user_prompt", "last_assistant")
_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)
DEFAULT_OLLAMA_KEEP_ALIVE = "30s"


def _launchctl_kickstart(label: str, plist_path: Optional[str] = None) -> None:
    """Start a user LaunchAgent by label without requiring it to RunAtLoad."""
    import os

    uid = os.getuid()
    try:
        subprocess.run(
            ["/bin/launchctl", "kickstart", f"gui/{uid}/{label}"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as e:
        detail = getattr(e, "stderr", "") or str(e)
        if plist_path:
            try:
                subprocess.run(
                    [
                        "/bin/launchctl",
                        "bootstrap",
                        f"gui/{uid}",
                        os.path.expanduser(plist_path),
                    ],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=10,
                )
                return
            except (OSError, subprocess.SubprocessError) as bootstrap_e:
                boot_detail = getattr(bootstrap_e, "stderr", "") or str(bootstrap_e)
                raise BackendError(
                    f"ollama: failed to start {label} "
                    f"(kickstart: {detail.strip()}; bootstrap: {boot_detail.strip()})"
                ) from bootstrap_e
        raise BackendError(f"ollama: failed to start {label} ({detail.strip()})") from e


def _strip_fence(s: str) -> str:
    s = s.strip()
    m = _FENCE_RE.match(s)
    return m.group(1) if m else s


class OllamaBackend:
    """LLMBackend implementation that calls a WoL-managed Ollama host."""

    name = "ollama"

    def __init__(
        self,
        *,
        rig: GpuRig,
        model: str,
        request_timeout_s: float = 600.0,
        wake_on_first_call: bool = True,
        launchd_label: Optional[str] = None,
        launchd_plist: Optional[str] = None,
        keep_alive: Optional[str] = None,
        required_keys: Optional[tuple] = None,
        optional_keys: Optional[tuple] = None,
    ) -> None:
        self.rig = rig
        self.model = model
        self.request_timeout_s = request_timeout_s
        self.wake_on_first_call = wake_on_first_call
        self.launchd_label = launchd_label
        self.launchd_plist = launchd_plist
        self.keep_alive = (
            keep_alive
            or (
                os.environ.get("DROVER_SUMMARIZER_OLLAMA_KEEP_ALIVE")
                or os.environ.get("NEXUS_SUMMARIZER_OLLAMA_KEEP_ALIVE")
            )
            or DEFAULT_OLLAMA_KEEP_ALIVE
        )
        self.required_keys = required_keys or _REQUIRED_KEYS
        self.optional_keys = optional_keys or _OPTIONAL_KEYS
        self._awoken = False  # tracks whether we've wake-checked in this process

    def ensure_ready(self) -> None:
        """Wake the rig if needed. Idempotent; safe to call before each batch."""
        try:
            if self.launchd_label:
                log.info(
                    "local Ollama cold start: kickstarting %s for model %s",
                    self.launchd_label,
                    self.model,
                )
                _launchctl_kickstart(self.launchd_label, self.launchd_plist)
            else:
                ensure_gpu_awake(self.rig)
            wait_for_ollama(
                self.rig.ollama_url,
                timeout_s=self.rig.wake_timeout_s,
                model=self.model,
                poll_interval_s=min(5.0, max(1.0, self.rig.poll_interval_s)),
            )
            self._warm_model()
        except GpuWakeError as e:
            raise BackendReadinessError(
                f"ollama readiness: local model cold start/model availability check failed ({e})"
            ) from e
        self._awoken = True

    def _warm_model(self) -> None:
        """Issue a tiny generate request so Ollama loads the model before jobs run."""
        url = f"{self.rig.ollama_url.rstrip('/')}/api/generate"
        body = {
            "model": self.model,
            "prompt": "Return an empty JSON object: {}",
            "stream": False,
            "format": "json",
            "keep_alive": self.keep_alive,
            "options": {"temperature": 0},
        }
        timeout = min(self.request_timeout_s, max(1.0, self.rig.wake_timeout_s))
        try:
            r = requests.post(url, json=body, timeout=timeout)
        except requests.Timeout as e:
            raise BackendReadinessError(
                f"ollama readiness: local model {self.model!r} cold-start warmup timed out "
                f"after {timeout:.0f}s"
            ) from e
        except requests.RequestException as e:
            raise BackendReadinessError(
                f"ollama readiness: local model {self.model!r} warmup HTTP error ({e})"
            ) from e
        if not r.ok:
            detail = r.text[:200]
            unavailable = (
                "unavailable" if r.status_code in (400, 404) else "warmup failed"
            )
            raise BackendReadinessError(
                f"ollama readiness: local model {self.model!r} {unavailable}: "
                f"{r.status_code} {detail}"
            )
        log.info(
            "local Ollama model %s warmed and ready at %s",
            self.model,
            self.rig.ollama_url,
        )

    def summarize(self, prompt: str) -> dict:
        if self.wake_on_first_call and not self._awoken:
            self.ensure_ready()

        url = f"{self.rig.ollama_url.rstrip('/')}/api/generate"
        body = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "keep_alive": self.keep_alive,
            "options": {"temperature": 0.2},
        }
        try:
            r = requests.post(url, json=body, timeout=self.request_timeout_s)
        except requests.RequestException as e:
            raise BackendError(f"ollama: HTTP error ({e})") from e
        if not r.ok:
            raise BackendError(f"ollama: {r.status_code} {r.text[:200]}")

        try:
            envelope = r.json()
        except ValueError as e:
            raise BackendError(f"ollama: response is not JSON ({e})") from e

        raw = envelope.get("response", "")
        if not raw:
            raise BackendError("ollama: empty response field")

        try:
            parsed = json.loads(_strip_fence(raw))
        except json.JSONDecodeError as e:
            raise BackendError(
                f"ollama: response payload is not JSON ({e}); raw={raw[:200]!r}"
            ) from e

        if not isinstance(parsed, dict):
            raise BackendError(
                f"ollama: response is not an object: {type(parsed).__name__}"
            )

        missing = [k for k in self.required_keys if k not in parsed]
        if missing:
            raise BackendError(f"ollama: response missing required keys: {missing}")

        out: dict = {k: parsed[k] for k in self.required_keys}
        for k in self.optional_keys:
            out[k] = parsed.get(k)
        return out
