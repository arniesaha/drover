"""Native, bounded model discovery through the local agy CLI."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from typing import Sequence

from .models import DiscoveredCatalog, MAX_MODELS, ModelOption
from .service import CatalogDiscoveryError

_MAX_OUTPUT_BYTES = 256 * 1024


class AgyCatalogAdapter:
    """Discover agy's native model list without probing or fabricating models."""

    def __init__(
        self,
        command: Sequence[str],
        accounts_path: Path | None = None,
        timeout_s: float = 5.0,
    ):
        self.command = tuple(command)
        self.accounts_path = (
            Path(accounts_path).expanduser()
            if accounts_path is not None
            else Path.home() / ".gemini" / "google_accounts.json"
        )
        self.timeout_s = timeout_s

    def cache_identity(self) -> str:
        executable = _executable_path(self.command)
        parts = ["command", *self.command, "executable", str(executable)]
        parts.extend(_stat_metadata(executable))
        parts.extend(
            ("accounts", str(self.accounts_path), *_stat_metadata(self.accounts_path))
        )
        return _fingerprint(parts)

    def discover(self) -> DiscoveredCatalog:
        account = self._account_label()
        rows = self._models()
        version = self._version()
        try:
            return DiscoveredCatalog(
                account_scope_material=f"agy|{account}",
                harness_version=version,
                models=rows,
            )
        except ValueError:
            raise CatalogDiscoveryError("protocol_error") from None

    def _models(self) -> tuple[ModelOption, ...]:
        if not self.command:
            raise CatalogDiscoveryError("unsupported")
        try:
            result = subprocess.run(
                [*self.command, "models"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_s,
                check=False,
            )
        except FileNotFoundError:
            raise CatalogDiscoveryError("unsupported") from None
        except subprocess.TimeoutExpired:
            raise CatalogDiscoveryError("timeout") from None
        except OSError:
            raise CatalogDiscoveryError("protocol_error") from None
        if result.returncode != 0 or _output_too_large(result.stdout):
            raise CatalogDiscoveryError("protocol_error")

        models: list[ModelOption] = []
        seen: set[str] = set()
        for line in result.stdout.splitlines():
            if line == "Fetching available models...":
                continue
            fields = line.split("\t")
            if len(fields) != 2:
                continue
            model_id, display_name = (field.strip() for field in fields)
            if not model_id or not display_name or model_id in seen:
                continue
            seen.add(model_id)
            try:
                model = ModelOption(id=model_id, display_name=display_name)
            except ValueError:
                continue
            models.append(model)
            if len(models) > MAX_MODELS:
                raise CatalogDiscoveryError("protocol_error")
        if not models:
            raise CatalogDiscoveryError("protocol_error")
        return tuple(models)

    def _version(self) -> str:
        if not self.command:
            raise CatalogDiscoveryError("protocol_error")
        try:
            result = subprocess.run(
                [*self.command, "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_s,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise CatalogDiscoveryError("protocol_error") from None
        if (
            result.returncode != 0
            or _output_too_large(result.stdout)
            or not result.stdout.strip()
        ):
            raise CatalogDiscoveryError("protocol_error")
        return result.stdout.strip()

    def _account_label(self) -> str:
        try:
            value = json.loads(self.accounts_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise CatalogDiscoveryError("not_authenticated") from None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise CatalogDiscoveryError("protocol_error") from None
        if not isinstance(value, dict):
            raise CatalogDiscoveryError("protocol_error")
        active = value.get("active")
        if isinstance(active, str) and active.strip():
            return active.strip()
        old = value.get("old")
        if isinstance(old, list):
            for label in old:
                if isinstance(label, str) and label.strip():
                    return label.strip()
        raise CatalogDiscoveryError("not_authenticated")


def _output_too_large(value: str) -> bool:
    return len(value.encode("utf-8")) > _MAX_OUTPUT_BYTES


def _stat_metadata(path: Path) -> tuple[str, ...]:
    try:
        result = path.stat()
    except OSError:
        return ("missing",)
    return (
        "present",
        str(result.st_dev),
        str(result.st_ino),
        str(result.st_mode),
        str(result.st_size),
        str(result.st_mtime_ns),
    )


def _executable_path(command: Sequence[str]) -> Path:
    if not command:
        return Path("")
    path = Path(command[0])
    if path.parent == Path("."):
        resolved = shutil.which(command[0])
        if resolved:
            return Path(resolved)
    return path


def _fingerprint(parts: Sequence[str]) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()
