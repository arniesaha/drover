"""Native, bounded model discovery through the local agy CLI."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import time
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
        _require_executable(self.command)
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
        returncode, output = _run_bounded(
            [*self.command, "models"],
            timeout_s=self.timeout_s,
            missing_category="unsupported",
        )
        if returncode != 0:
            raise CatalogDiscoveryError("protocol_error")

        models: list[ModelOption] = []
        seen: set[str] = set()
        for line in output.decode("utf-8", errors="replace").splitlines():
            if line == "Fetching available models...":
                continue
            fields = line.split("\t")
            if len(fields) != 2:
                continue
            model_id, display_name = fields
            if (
                not model_id
                or not display_name
                or not model_id.strip()
                or not display_name.strip()
                or model_id in seen
            ):
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
        returncode, output = _run_bounded(
            [*self.command, "--version"],
            timeout_s=self.timeout_s,
            missing_category="protocol_error",
        )
        version = output.decode("utf-8", errors="replace").strip()
        if returncode != 0 or not version:
            raise CatalogDiscoveryError("protocol_error")
        return version

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


def _run_bounded(
    command: Sequence[str],
    *,
    timeout_s: float,
    missing_category: str,
    os_error_category: str = "protocol_error",
) -> tuple[int, bytes]:
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        raise CatalogDiscoveryError(missing_category) from None
    except OSError:
        raise CatalogDiscoveryError(os_error_category) from None
    except (TypeError, ValueError):
        raise CatalogDiscoveryError("protocol_error") from None

    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    output = bytearray()
    deadline = time.monotonic() + timeout_s
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _stop_process(process)
                raise CatalogDiscoveryError("timeout")
            ready = selector.select(remaining)
            if not ready:
                _stop_process(process)
                raise CatalogDiscoveryError("timeout")
            for key, _ in ready:
                chunk = os.read(
                    key.fd, min(65_536, _MAX_OUTPUT_BYTES + 1 - len(output))
                )
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output.extend(chunk)
                if len(output) > _MAX_OUTPUT_BYTES:
                    _stop_process(process)
                    raise CatalogDiscoveryError("protocol_error")
        remaining = deadline - time.monotonic()
        try:
            returncode = process.wait(timeout=max(remaining, 0))
        except subprocess.TimeoutExpired:
            _stop_process(process)
            raise CatalogDiscoveryError("timeout") from None
        return returncode, bytes(output)
    finally:
        selector.close()
        process.stdout.close()


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        try:
            process.terminate()
        except OSError:
            pass
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass


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


def _require_executable(command: Sequence[str]) -> None:
    if not command:
        raise CatalogDiscoveryError("unsupported")
    try:
        supplied = Path(command[0])
    except (TypeError, ValueError):
        raise CatalogDiscoveryError("protocol_error") from None
    if supplied.parent == Path("."):
        resolved = shutil.which(command[0])
        if resolved is None:
            raise CatalogDiscoveryError("unsupported")
        supplied = Path(resolved)
    try:
        supplied.stat()
    except FileNotFoundError:
        raise CatalogDiscoveryError("unsupported") from None
    except OSError:
        raise CatalogDiscoveryError("protocol_error") from None


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
