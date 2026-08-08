"""Wake-on-LAN helper for the GPU rig.

Always wakes via a configured relay rather than sending magic packets
directly. The relay owns network routing, bookkeeping, and throttling.

After a wake request, ``wait_for_ollama`` polls ``/api/tags`` until it
responds 200 or the deadline expires. It can also require a configured model
to appear in the tags response before returning. Cold-start on the rig is
typically 30-90s, so callers should batch their work to amortize.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

import requests

log = logging.getLogger("drover.wol")


class GpuWakeError(RuntimeError):
    """Wake request failed or Ollama did not become reachable in time."""


@dataclass(frozen=True)
class GpuRig:
    """Connection details for a Wake-on-LAN-managed Ollama host."""

    relay_url: str  # e.g. "http://gpu-relay.private:9753"
    ollama_url: str  # e.g. "http://gpu-host.private:11434"
    wake_timeout_s: float = 120.0
    poll_interval_s: float = 5.0


def _ollama_healthy(ollama_url: str, *, timeout_s: float = 3.0) -> bool:
    try:
        r = requests.get(f"{ollama_url.rstrip('/')}/api/tags", timeout=timeout_s)
        return r.status_code == 200
    except requests.RequestException:
        return False


def _ollama_model_ready(
    ollama_url: str,
    *,
    model: Optional[str] = None,
    timeout_s: float = 3.0,
) -> bool:
    try:
        r = requests.get(f"{ollama_url.rstrip('/')}/api/tags", timeout=timeout_s)
    except requests.RequestException:
        return False
    if r.status_code != 200:
        return False
    if not model:
        return True
    try:
        payload = r.json()
    except ValueError:
        return False
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return False

    def _matches(candidate: str) -> bool:
        return candidate == model or candidate == f"{model}:latest"

    return any(
        isinstance(entry, dict)
        and isinstance(entry.get("name"), str)
        and _matches(entry["name"])
        for entry in models
    )


def wake_via_relay(relay_url: str, *, timeout_s: float = 5.0) -> None:
    """POST a wake request to the configured relay."""
    url = f"{relay_url.rstrip('/')}/wake"
    try:
        r = requests.get(url, timeout=timeout_s)
    except requests.RequestException as e:
        raise GpuWakeError(f"WoL relay {url} unreachable: {e}") from e
    if not r.ok:
        raise GpuWakeError(f"WoL relay returned {r.status_code}: {r.text[:200]}")


def wait_for_ollama(
    ollama_url: str,
    *,
    timeout_s: float,
    model: Optional[str] = None,
    poll_interval_s: float = 5.0,
    sleep: Optional[callable] = None,
) -> None:
    """Block until Ollama responds on /api/tags and optional model is present."""
    sleep = sleep or time.sleep
    deadline = time.monotonic() + timeout_s
    service_reachable = False
    while time.monotonic() < deadline:
        try:
            r = requests.get(f"{ollama_url.rstrip('/')}/api/tags", timeout=3.0)
        except requests.RequestException:
            sleep(poll_interval_s)
            continue
        if r.status_code == 200:
            service_reachable = True
            if not model:
                return
            try:
                payload = r.json()
            except ValueError:
                payload = None
            models = payload.get("models") if isinstance(payload, dict) else None
            if isinstance(models, list):
                names = [
                    entry.get("name") for entry in models if isinstance(entry, dict)
                ]
                if model in names or f"{model}:latest" in names:
                    return
        sleep(poll_interval_s)
    if model and service_reachable:
        raise GpuWakeError(
            f"Ollama at {ollama_url} is reachable but model {model!r} "
            f"was unavailable within {timeout_s:.0f}s"
        )
    suffix = f" with model {model!r}" if model else ""
    raise GpuWakeError(
        f"Ollama at {ollama_url}{suffix} did not become reachable within {timeout_s:.0f}s"
    )


def ensure_gpu_awake(rig: GpuRig, *, sleep: Optional[callable] = None) -> bool:
    """Make sure the GPU rig is reachable. Returns True if a wake was needed.

    Idempotent: a no-op if Ollama already responds. Otherwise calls the relay
    and waits for Ollama to come up.
    """
    if _ollama_healthy(rig.ollama_url):
        return False
    log.info("GPU rig appears asleep; waking via relay %s", rig.relay_url)
    wake_via_relay(rig.relay_url)
    wait_for_ollama(
        rig.ollama_url,
        timeout_s=rig.wake_timeout_s,
        model=None,
        poll_interval_s=rig.poll_interval_s,
        sleep=sleep,
    )
    log.info("GPU rig awake — Ollama at %s reachable", rig.ollama_url)
    return True
