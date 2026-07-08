"""Embeddings clients.

Drover can embed session summaries through a remote OpenAI-compatible
embeddings API when configured, then a Mac-local Ollama daemon, and finally
falls back to Ollama's ``/api/embed`` on the WoL-managed GPU rig for
offline/local operation.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from typing import Iterable, Optional

import requests

from drover.server.summarizer.backends.types import BackendError
from drover.server.wol import GpuRig, GpuWakeError, ensure_gpu_awake, wait_for_ollama

log = logging.getLogger("drover.embeddings.client")


DEFAULT_EMBED_MODEL = "nomic-embed-text"
DEFAULT_API_EMBED_MODEL = "text-embedding-3-small"
DEFAULT_OLLAMA_KEEP_ALIVE = "5m"
DEFAULT_MAC_OLLAMA_LAUNCHD_LABEL = "com.drover.mac-ollama-embeddings"
DEFAULT_MAC_OLLAMA_LAUNCHD_PLIST = (
    "/Users/arnabmac/Library/LaunchAgents/com.drover.mac-ollama-embeddings.plist"
)


def _launchctl_kickstart(label: str, plist_path: Optional[str] = None) -> None:
    """Start a user LaunchAgent by label without requiring it to RunAtLoad."""
    uid = os.getuid()
    cmd = ["/bin/launchctl", "kickstart", f"gui/{uid}/{label}"]
    try:
        subprocess.run(
            cmd,
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
                    ["/bin/launchctl", "bootstrap", f"gui/{uid}", plist_path],
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
                    f"embeddings: failed to start {label} "
                    f"(kickstart: {detail.strip()}; bootstrap: {boot_detail.strip()})"
                ) from bootstrap_e
        raise BackendError(
            f"embeddings: failed to start {label} ({detail.strip()})"
        ) from e


class ApiEmbedder:
    """OpenAI/Voyage-compatible embeddings client.

    The endpoint is intentionally configured as a base URL rather than tied
    to a vendor. Many embedding providers expose the common
    ``POST /embeddings`` shape: ``{"model": ..., "input": [...]}`` with a
    ``data[].embedding`` response.
    """

    name = "api-embed"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str = DEFAULT_API_EMBED_MODEL,
        request_timeout_s: float = 120.0,
    ) -> None:
        if not base_url:
            raise BackendError("embeddings: api_base_url not configured")
        if not api_key:
            raise BackendError("embeddings: api_key not configured")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.request_timeout_s = request_timeout_s

    def ensure_ready(self) -> None:
        return None

    def embed(self, text: str) -> list[float]:
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        url = f"{self.base_url}/embeddings"
        body = {"model": self.model, "input": texts}
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            r = requests.post(
                url, json=body, headers=headers, timeout=self.request_timeout_s
            )
        except requests.RequestException as e:
            raise BackendError(f"embeddings api: HTTP error ({e})") from e
        if not r.ok:
            raise BackendError(f"embeddings api: {r.status_code} {r.text[:200]}")
        try:
            payload = r.json()
        except ValueError as e:
            raise BackendError(f"embeddings api: response is not JSON ({e})") from e

        data = payload.get("data")
        if not isinstance(data, list) or len(data) != len(texts):
            got = len(data) if isinstance(data, list) else type(data).__name__
            raise BackendError(
                f"embeddings api: expected {len(texts)} vectors, got {got}"
            )
        vectors: list[list[float]] = []
        for item in data:
            if not isinstance(item, dict) or not isinstance(
                item.get("embedding"), list
            ):
                raise BackendError(
                    "embeddings api: response item missing embedding list"
                )
            vectors.append([float(x) for x in item["embedding"]])
        return vectors


@dataclass(frozen=True)
class EmbeddingBackendConfig:
    """Configuration for API-first embeddings with local Ollama fallback."""

    api_base_url: Optional[str] = None
    api_key: Optional[str] = None
    api_model: str = DEFAULT_API_EMBED_MODEL
    mac_ollama_url: Optional[str] = None
    gpu_rig: Optional[GpuRig] = None
    local_model: str = DEFAULT_EMBED_MODEL
    mac_ollama_launchd_label: Optional[str] = DEFAULT_MAC_OLLAMA_LAUNCHD_LABEL
    mac_ollama_launchd_plist: Optional[str] = DEFAULT_MAC_OLLAMA_LAUNCHD_PLIST
    ollama_keep_alive: str = DEFAULT_OLLAMA_KEEP_ALIVE

    @property
    def has_api_embedder(self) -> bool:
        return bool(self.api_base_url and self.api_key)

    @classmethod
    def from_runtime(
        cls,
        *,
        api_base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        api_model: Optional[str] = None,
        mac_ollama_url: Optional[str] = None,
        gpu_rig: Optional[GpuRig] = None,
        local_model: Optional[str] = None,
        mac_ollama_launchd_label: Optional[str] = None,
        mac_ollama_launchd_plist: Optional[str] = None,
        ollama_keep_alive: Optional[str] = None,
    ) -> "EmbeddingBackendConfig":
        return cls(
            api_base_url=api_base_url
            or (
                os.environ.get("DROVER_EMBEDDINGS_API_BASE_URL")
                or os.environ.get("NEXUS_EMBEDDINGS_API_BASE_URL")
            )
            or None,
            api_key=api_key
            or (
                os.environ.get("DROVER_EMBEDDINGS_API_KEY")
                or os.environ.get("NEXUS_EMBEDDINGS_API_KEY")
            )
            or None,
            api_model=api_model
            or (
                os.environ.get("DROVER_EMBEDDINGS_API_MODEL")
                or os.environ.get("NEXUS_EMBEDDINGS_API_MODEL")
            )
            or DEFAULT_API_EMBED_MODEL,
            mac_ollama_url=mac_ollama_url
            or (
                os.environ.get("DROVER_EMBEDDINGS_MAC_OLLAMA_URL")
                or os.environ.get("NEXUS_EMBEDDINGS_MAC_OLLAMA_URL")
            )
            or None,
            gpu_rig=gpu_rig,
            local_model=local_model
            or (
                os.environ.get("DROVER_EMBEDDINGS_LOCAL_MODEL")
                or os.environ.get("NEXUS_EMBEDDINGS_LOCAL_MODEL")
            )
            or DEFAULT_EMBED_MODEL,
            mac_ollama_launchd_label=mac_ollama_launchd_label
            or (
                os.environ.get("DROVER_EMBEDDINGS_MAC_OLLAMA_LAUNCHD_LABEL")
                or os.environ.get("NEXUS_EMBEDDINGS_MAC_OLLAMA_LAUNCHD_LABEL")
            )
            or DEFAULT_MAC_OLLAMA_LAUNCHD_LABEL,
            mac_ollama_launchd_plist=mac_ollama_launchd_plist
            or (
                os.environ.get("DROVER_EMBEDDINGS_MAC_OLLAMA_LAUNCHD_PLIST")
                or os.environ.get("NEXUS_EMBEDDINGS_MAC_OLLAMA_LAUNCHD_PLIST")
            )
            or DEFAULT_MAC_OLLAMA_LAUNCHD_PLIST,
            ollama_keep_alive=ollama_keep_alive
            or (
                os.environ.get("DROVER_EMBEDDINGS_OLLAMA_KEEP_ALIVE")
                or os.environ.get("NEXUS_EMBEDDINGS_OLLAMA_KEEP_ALIVE")
            )
            or DEFAULT_OLLAMA_KEEP_ALIVE,
        )

    def select_embedder(self) -> Optional["ApiEmbedder | OllamaEmbedder"]:
        if self.has_api_embedder:
            return ApiEmbedder(
                base_url=self.api_base_url or "",
                api_key=self.api_key or "",
                model=self.api_model,
            )
        if self.mac_ollama_url:
            log.warning(
                "embedding backend selected by fallback: mac-local Ollama (API embedder unavailable)"
            )
            return OllamaEmbedder(
                ollama_url=self.mac_ollama_url,
                model=self.local_model,
                wake_on_first_call=True,
                launchd_label=self.mac_ollama_launchd_label,
                launchd_plist=self.mac_ollama_launchd_plist,
                keep_alive=self.ollama_keep_alive,
            )
        if self.gpu_rig is not None:
            log.warning(
                "embedding backend selected by fallback: GPU Ollama (API and Mac-local embedders unavailable)"
            )
            return OllamaEmbedder(
                rig=self.gpu_rig,
                model=self.local_model,
                keep_alive=self.ollama_keep_alive,
            )
        return None


class OllamaEmbedder:
    """Wraps POST /api/embed against an Ollama host."""

    name = "ollama-embed"

    def __init__(
        self,
        *,
        rig: Optional[GpuRig] = None,
        ollama_url: Optional[str] = None,
        model: str = DEFAULT_EMBED_MODEL,
        request_timeout_s: float = 120.0,
        wake_on_first_call: bool = True,
        launchd_label: Optional[str] = None,
        launchd_plist: Optional[str] = None,
        keep_alive: str = DEFAULT_OLLAMA_KEEP_ALIVE,
    ) -> None:
        if rig is None and not ollama_url:
            raise BackendError("embeddings: ollama_url or GPU rig required")
        self.rig = rig
        self.ollama_url = ollama_url or (rig.ollama_url if rig is not None else "")
        self.model = model
        self.request_timeout_s = request_timeout_s
        self.wake_on_first_call = wake_on_first_call
        self.launchd_label = launchd_label
        self.launchd_plist = launchd_plist
        self.keep_alive = keep_alive
        self._awoken = False

    def ensure_ready(self) -> None:
        if self.rig is None:
            if self.launchd_label:
                _launchctl_kickstart(self.launchd_label, self.launchd_plist)
                wait_for_ollama(
                    self.ollama_url,
                    timeout_s=self.request_timeout_s,
                    model=self.model,
                    poll_interval_s=1.0,
                )
            self._awoken = True
            return
        try:
            ensure_gpu_awake(self.rig)
        except GpuWakeError as e:
            raise BackendError(f"embeddings: GPU wake failed ({e})") from e
        self._awoken = True

    def embed(self, text: str) -> list[float]:
        """Return a single embedding vector for ``text``."""
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if self.wake_on_first_call and not self._awoken:
            self.ensure_ready()

        url = f"{self.ollama_url.rstrip('/')}/api/embed"
        body = {"model": self.model, "input": texts, "keep_alive": self.keep_alive}
        try:
            r = requests.post(url, json=body, timeout=self.request_timeout_s)
        except requests.RequestException as e:
            raise BackendError(f"embeddings: HTTP error ({e})") from e
        if not r.ok:
            raise BackendError(f"embeddings: {r.status_code} {r.text[:200]}")

        try:
            payload = r.json()
        except ValueError as e:
            raise BackendError(f"embeddings: response is not JSON ({e})") from e

        embs = payload.get("embeddings")
        if not isinstance(embs, list) or len(embs) != len(texts):
            raise BackendError(
                f"embeddings: expected {len(texts)} vectors, got {len(embs) if isinstance(embs, list) else type(embs).__name__}"
            )
        # Coerce to list[list[float]]
        return [[float(x) for x in v] for v in embs]
