"""Validated command-plane contract for drive-capable harnesses.

This registry is deliberately independent of the current driver factories and
host capability payload. Existing harnesses move onto it in the next slice.
Collect ``Source`` objects are a separate, observe-only contract.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

from drover.server.harness.structured.driver import EmitFn

LaunchMode = Literal["structured", "pty"]
Operation = Literal[
    "structured",
    "pty",
    "approvals",
    "interrupt",
    "native_resume",
    "model_catalog",
    "usage",
    "worktree",
    "attachments",
    "interactive_auth",
]


@dataclass(frozen=True)
class HarnessCapabilities:
    """Executable operations, with attachment MIME types rather than a broad flag."""

    launch_modes: frozenset[LaunchMode]
    approvals: bool = False
    interrupt: bool = False
    native_resume: bool = False
    model_catalog: bool = False
    usage: bool = False
    worktree: bool = False
    attachments: frozenset[str] = frozenset()
    interactive_auth: bool = False


@dataclass(frozen=True)
class LaunchRequest:
    cwd: str | None = None
    command: tuple[str, ...] | None = None
    native_session_id: str | None = None


@dataclass(frozen=True)
class AdapterHealth:
    available: bool
    reason: str | None = None


class InvalidHarnessAdapter(ValueError):
    """An adapter cannot safely be offered as a drive target."""


class UnsupportedHarnessOperation(ValueError):
    """A valid adapter does not declare the requested operation."""


class HarnessAdapter(ABC):
    """One provider-neutral command boundary; native wire details stay inside.

    The session manager owns idempotence: it passes a supplied client_turn_id
    through as ``turn_id`` and does not dispatch a previously accepted key.
    Adapters emit the existing StructuredMessage vocabulary through ``emit``.
    Optional hooks are deliberately closed by default and may be overridden
    only when their capability is declared.
    """

    id: str
    display_name: str
    capabilities: HarnessCapabilities

    @abstractmethod
    def default_command(self) -> list[str]: ...

    def build_command(self, request: LaunchRequest) -> list[str]:
        raise UnsupportedHarnessOperation("pty launch is unsupported")

    def start(self, request: LaunchRequest, emit: EmitFn) -> object:
        raise UnsupportedHarnessOperation("structured launch is unsupported")

    @abstractmethod
    def send_turn(
        self,
        driver: object,
        text: str,
        turn_id: str,
        *,
        model: str | None = None,
        thinking_effort: str | None = None,
    ) -> None: ...

    @abstractmethod
    def close(self, driver: object) -> None: ...

    @abstractmethod
    def health(self) -> AdapterHealth: ...

    def answer_permission(
        self, driver: object, request_id: str, decision: str, note: str | None
    ) -> None:
        raise UnsupportedHarnessOperation("approvals are unsupported")

    def interrupt(self, driver: object) -> None:
        raise UnsupportedHarnessOperation("interrupt is unsupported")

    def resume(self, request: LaunchRequest, emit: EmitFn) -> object:
        raise UnsupportedHarnessOperation("native resume is unsupported")

    def model_catalog_adapter(self) -> object:
        raise UnsupportedHarnessOperation("model catalog is unsupported")

    def usage(self, driver: object) -> object:
        raise UnsupportedHarnessOperation("usage is unsupported")

    def worktree_policy(self) -> object:
        raise UnsupportedHarnessOperation("worktree isolation is unsupported")

    def send_attachments(
        self, driver: object, text: str, turn_id: str, attachments: list[dict]
    ) -> None:
        raise UnsupportedHarnessOperation("attachments are unsupported")

    def auth_adapter(self) -> object:
        raise UnsupportedHarnessOperation("interactive auth is unsupported")


_OPTIONAL_METHODS = {
    "approvals": "answer_permission",
    "interrupt": "interrupt",
    "native_resume": "resume",
    "model_catalog": "model_catalog_adapter",
    "usage": "usage",
    "worktree": "worktree_policy",
    "attachments": "send_attachments",
    "interactive_auth": "auth_adapter",
}
_LAUNCH_METHODS = {"structured": "start", "pty": "build_command"}
_HARNESS_ID = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")


class HarnessAdapterRegistry:
    """A stable harness ID maps to one validated drive adapter."""

    def __init__(self, adapters: list[HarnessAdapter] | None = None) -> None:
        self._adapters: dict[str, HarnessAdapter] = {}
        for adapter in adapters or ():
            self.register(adapter)

    def register(self, adapter: HarnessAdapter) -> None:
        if not isinstance(adapter, HarnessAdapter):
            raise InvalidHarnessAdapter("drive adapter must implement HarnessAdapter")
        harness_id = getattr(adapter, "id", None)
        if not isinstance(harness_id, str) or not _HARNESS_ID.fullmatch(harness_id):
            raise InvalidHarnessAdapter(f"invalid harness ID: {harness_id!r}")
        if harness_id in self._adapters:
            raise InvalidHarnessAdapter(f"duplicate harness ID: {harness_id}")
        display_name = getattr(adapter, "display_name", None)
        if not isinstance(display_name, str) or not display_name.strip():
            raise InvalidHarnessAdapter(f"{harness_id}: display_name is required")
        capabilities = getattr(adapter, "capabilities", None)
        if not isinstance(capabilities, HarnessCapabilities):
            raise InvalidHarnessAdapter(f"{harness_id}: invalid capabilities")
        modes = capabilities.launch_modes
        if (
            not isinstance(modes, frozenset)
            or not modes
            or not modes <= set(_LAUNCH_METHODS)
        ):
            raise InvalidHarnessAdapter(f"{harness_id}: invalid launch_modes")
        for method in ("default_command", "send_turn", "close", "health"):
            if not callable(getattr(adapter, method, None)):
                raise InvalidHarnessAdapter(f"{harness_id}: {method} must be callable")
        if not isinstance(capabilities.attachments, frozenset) or any(
            not isinstance(value, str) or not value.strip()
            for value in capabilities.attachments
        ):
            raise InvalidHarnessAdapter(f"{harness_id}: invalid attachments")
        for name in _OPTIONAL_METHODS:
            value = getattr(capabilities, name)
            if name != "attachments" and type(value) is not bool:
                raise InvalidHarnessAdapter(f"{harness_id}: {name} must be a bool")
            enabled = bool(value)
            method = _OPTIONAL_METHODS[name]
            hook = getattr(type(adapter), method, None)
            if not callable(hook):
                raise InvalidHarnessAdapter(f"{harness_id}: {method} must be callable")
            implemented = hook is not getattr(HarnessAdapter, method)
            if enabled != implemented:
                raise InvalidHarnessAdapter(
                    f"{harness_id}: {method} implementation contradicts {name} capability"
                )
        for mode, method in _LAUNCH_METHODS.items():
            hook = getattr(type(adapter), method, None)
            if not callable(hook):
                raise InvalidHarnessAdapter(f"{harness_id}: {method} must be callable")
            implemented = hook is not getattr(HarnessAdapter, method)
            if (mode in modes) != implemented:
                raise InvalidHarnessAdapter(
                    f"{harness_id}: {method} implementation contradicts {mode} launch mode"
                )
        self._adapters[harness_id] = adapter

    def register_all(
        self, adapters: list[HarnessAdapter]
    ) -> list[InvalidHarnessAdapter]:
        """Load independent adapters, keeping bad declarations unavailable."""
        errors: list[InvalidHarnessAdapter] = []
        for adapter in adapters:
            try:
                self.register(adapter)
            except InvalidHarnessAdapter as exc:
                errors.append(exc)
        return errors

    def resolve(
        self, harness_id: str, *, operation: Operation | None = None
    ) -> HarnessAdapter:
        adapter = self._adapters[harness_id]
        if operation is None:
            return adapter
        capabilities = adapter.capabilities
        if operation in _LAUNCH_METHODS:
            supported = operation in capabilities.launch_modes
        elif operation in _OPTIONAL_METHODS:
            supported = bool(getattr(capabilities, operation))
        else:
            raise ValueError(f"unknown harness operation: {operation}")
        if not supported:
            raise UnsupportedHarnessOperation(
                f"{harness_id} does not support {operation}"
            )
        return adapter
