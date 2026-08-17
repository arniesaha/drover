"""Opaque, host-local account scope identifiers."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import tempfile
from pathlib import Path

_SECRET_SIZE = 32
_DEFAULT_SECRET_PATH = Path.home() / ".drover" / "model-catalog-scope.key"


class AccountScopeIDs:
    def __init__(self, *, secret: bytes | None = None, path: Path | None = None):
        loaded_secret = secret if secret is not None else _load_or_create_secret(path)
        if not isinstance(loaded_secret, bytes) or len(loaded_secret) != _SECRET_SIZE:
            raise ValueError("model catalog scope secret must be exactly 32 bytes")
        self._secret = loaded_secret

    def for_material(self, material: str) -> str:
        if not isinstance(material, str) or not material.strip():
            raise ValueError("account scope material is required")
        return hmac.new(
            self._secret, material.encode("utf-8"), hashlib.sha256
        ).hexdigest()


def _load_or_create_secret(path: Path | None = None) -> bytes:
    secret_path = path if path is not None else _DEFAULT_SECRET_PATH
    secret_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    try:
        existing = secret_path.read_bytes()
    except FileNotFoundError:
        return _create_secret(secret_path)
    _validate_secret(existing, secret_path)
    os.chmod(secret_path, 0o600)
    return existing


def _create_secret(secret_path: Path) -> bytes:
    secret = secrets.token_bytes(_SECRET_SIZE)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=secret_path.parent, prefix=f".{secret_path.name}.", suffix=".tmp"
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as temporary_file:
            temporary_file.write(secret)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        try:
            os.link(temporary_path, secret_path)
        except FileExistsError:
            existing = secret_path.read_bytes()
            _validate_secret(existing, secret_path)
            os.chmod(secret_path, 0o600)
            return existing
        return secret
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _validate_secret(secret: bytes, path: Path) -> None:
    if len(secret) != _SECRET_SIZE:
        raise ValueError(f"invalid model catalog scope secret at {path}")
