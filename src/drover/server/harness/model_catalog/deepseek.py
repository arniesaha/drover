"""DeepSeek Harness model discovery through its host-scoped RPC catalog."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Sequence

from drover.server.harness.structured.deepseek import (
    DeepSeekApiClient,
    DeepSeekApiError,
    version,
)

from .models import MAX_MODELS, DiscoveredCatalog, ModelOption, ReasoningOptions
from .service import CatalogDiscoveryError


class DeepSeekCatalogAdapter:
    def __init__(
        self,
        command: Sequence[str],
        api: DeepSeekApiClient | None = None,
    ) -> None:
        self.command = tuple(command)
        self.api = api or DeepSeekApiClient()

    def cache_identity(self) -> str:
        parts = [self.api.base_url, *self.command]
        if self.command:
            try:
                stat = Path(self.command[0]).stat()
                parts.extend([str(stat.st_size), str(stat.st_mtime_ns)])
            except OSError:
                parts.append("missing")
        return hashlib.sha256("\0".join(parts).encode()).hexdigest()

    def discover(self) -> DiscoveredCatalog:
        try:
            response = self.api.call("llm.models", {})
            harness_version = version(self.command)
        except DeepSeekApiError as exc:
            message = str(exc).lower()
            reason = "timeout" if "timed out" in message else "offline"
            raise CatalogDiscoveryError(reason) from exc
        default_id = os.environ.get(
            "DROVER_DEEPSEEK_DEFAULT_MODEL", "ollama/qwen3.5:35b-a3b"
        )
        models: list[ModelOption] = []
        groups = response.get("groups")
        for group in groups if isinstance(groups, list) else []:
            if not isinstance(group, dict):
                continue
            provider = group.get("id")
            provider_name = group.get("name") or provider
            rows = group.get("models")
            if not isinstance(provider, str) or not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                    continue
                model_id = f"{provider}/{row['id']}"
                try:
                    # _reasoning() builds a ReasoningOptions, whose validation
                    # is what rejects an unsupported default effort -- keep it
                    # inside the guard so one unusable model is skipped rather
                    # than downgrading the whole host's catalog to stale.
                    reasoning = _reasoning(row.get("reasoning"))
                    models.append(
                        ModelOption(
                            id=model_id,
                            display_name=f"{row.get('name') or row['id']} · {provider_name}",
                            is_default=model_id == default_id,
                            reasoning=reasoning,
                        )
                    )
                except ValueError:
                    continue
                if len(models) > MAX_MODELS:
                    raise CatalogDiscoveryError("protocol_error")
        if not models:
            raise CatalogDiscoveryError("protocol_error")
        if not any(model.is_default for model in models):
            models[0] = ModelOption(
                id=models[0].id,
                display_name=models[0].display_name,
                description=models[0].description,
                is_default=True,
                reasoning=models[0].reasoning,
            )
        try:
            return DiscoveredCatalog(
                account_scope_material=f"deepseek-harness|{self.api.base_url}",
                harness_version=harness_version,
                models=tuple(models),
            )
        except ValueError:
            # A duplicate provider/model pair (or any other record the shared
            # validation rejects) is a protocol problem with this harness, not
            # an unclassified crash for the service catch-all to guess at.
            raise CatalogDiscoveryError("protocol_error") from None


def _reasoning(value: object) -> ReasoningOptions | None:
    if not isinstance(value, dict):
        return None
    efforts = value.get("efforts")
    if not isinstance(efforts, list):
        return None
    supported = tuple(
        row["id"]
        for row in efforts
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    )
    if not supported:
        return None
    default = value.get("defaultEffort")
    return ReasoningOptions(
        supported=supported,
        default=default if isinstance(default, str) else None,
    )
