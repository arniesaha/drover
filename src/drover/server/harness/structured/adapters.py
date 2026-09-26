"""Compatibility adapters for the existing structured drivers.

These classes bind provider-specific construction and host policy to the
validated drive registry. Native wire parsing stays in each existing driver.
"""

from __future__ import annotations

from typing import Any

from drover.server.harness.adapters import (
    AdapterHealth,
    HarnessAdapter,
    HarnessAdapterRegistry,
    HarnessCapabilities,
    LaunchRequest,
)
from drover.server.harness.auth import (
    CommandAuthAdapter,
    HarnessAuthStatus,
    StaticAuthAdapter,
    _command_with_args,
    _resolve_login_command,
)
from drover.server.harness.model_catalog import (
    AgyCatalogAdapter,
    ClaudeCatalogAdapter,
    CodexCatalogAdapter,
    DeepSeekCatalogAdapter,
)
from drover.server.harness.structured import agy, claude, codex, deepseek
from drover.server.harness.structured.driver import EmitFn
from drover.server.staging_credentials import is_staging, read_api_key

_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})


class _StructuredAdapter(HarnessAdapter):
    """Shared delegation that preserves manager ordering and driver semantics."""

    recover_after_restart = False
    persistent_turn_guard = False
    turn_preferences_mutable = True

    def apply_preferences(
        self, command: list[str], model: str | None, thinking_effort: str | None
    ) -> list[str]:
        return list(command)

    def send_turn(
        self,
        driver: Any,
        text: str,
        turn_id: str,
        *,
        images: list | None = None,
        model: str | None = None,
        thinking_effort: str | None = None,
    ) -> None:
        driver.send_turn(
            text,
            turn_id,
            images=images,
            model=model,
            thinking_effort=thinking_effort,
        )

    def send_attachments(
        self,
        driver: Any,
        text: str,
        turn_id: str,
        attachments: list[dict],
        *,
        model: str | None = None,
        thinking_effort: str | None = None,
    ) -> None:
        self.send_turn(
            driver,
            text,
            turn_id,
            images=attachments,
            model=model,
            thinking_effort=thinking_effort,
        )

    def close(self, driver: Any) -> None:
        driver.close()

    def health(self) -> AdapterHealth:
        # Executable availability is host-specific and remains in the resolved
        # preset; this reports that the adapter implementation is usable.
        return AdapterHealth(available=True)

    def interrupt(self, driver: Any) -> None:
        driver.interrupt()

    def resume(self, request: LaunchRequest, emit: EmitFn) -> object:
        return self.start(request, emit)

    def model_catalog_adapter(self, executable: str) -> object:
        return self.catalog_type((executable, *self.catalog_suffix))


class _WorktreeAdapter:
    def worktree_policy(self) -> str:
        return "isolate_if_git"


class ClaudeCodeAdapter(_StructuredAdapter):
    id = "claude-code"
    display_name = "Claude Code CLI"
    capabilities = HarnessCapabilities(
        launch_modes=frozenset({"structured"}),
        approvals=True,
        interrupt=True,
        native_resume=True,
        model_catalog=True,
        attachments=_IMAGE_TYPES,
        interactive_auth=True,
    )
    catalog_type = ClaudeCatalogAdapter
    catalog_suffix: tuple[str, ...] = ()
    recover_after_restart = True
    persistent_turn_guard = True
    turn_preferences_mutable = False

    def apply_preferences(
        self, command: list[str], model: str | None, thinking_effort: str | None
    ) -> list[str]:
        preferred = list(command)
        if model:
            preferred.extend(["--model", model])
        if thinking_effort:
            preferred.extend(["--effort", thinking_effort])
        return preferred

    def default_command(self) -> list[str]:
        return claude.default_command()

    def start(self, request: LaunchRequest, emit: EmitFn) -> claude.ClaudeDriver:
        command = list(request.command) if request.command else self.default_command()
        return claude.ClaudeDriver(
            claude.resume_command(command, request.native_session_id),
            request.cwd,
            emit,
            env=claude.child_env(),
        )

    def answer_permission(
        self, driver: Any, request_id: str, decision: str, note: str | None
    ) -> None:
        driver.answer_permission(request_id, decision, note)

    def auth_adapter(self, *, shell: str | None = None) -> object | None:
        if is_staging():
            try:
                read_api_key()
                state = "authenticated"
            except (OSError, ValueError):
                state = "unauthenticated"
            return StaticAuthAdapter(
                self.id,
                HarnessAuthStatus(
                    self.id,
                    state,
                    detail="Dedicated staging API key; local provisioning only",
                ),
                sign_in="unsupported",
            )
        command = _resolve_login_command("claude", shell=shell)
        if command is None:
            return None
        return CommandAuthAdapter(
            self.id,
            _command_with_args(command, "auth", "status", "--json"),
            _command_with_args(command, "auth", "login"),
            requires_pty=True,
        )


class CodexAdapter(_WorktreeAdapter, _StructuredAdapter):
    id = "codex"
    display_name = "Codex CLI"
    capabilities = HarnessCapabilities(
        launch_modes=frozenset({"structured"}),
        interrupt=True,
        native_resume=True,
        model_catalog=True,
        worktree=True,
        attachments=_IMAGE_TYPES,
        interactive_auth=True,
    )
    catalog_type = CodexCatalogAdapter
    catalog_suffix = ("app-server", "--stdio")
    recover_after_restart = True

    def default_command(self) -> list[str]:
        return codex.default_command()

    def start(self, request: LaunchRequest, emit: EmitFn) -> codex.CodexDriver:
        command = list(request.command) if request.command else self.default_command()
        return codex.CodexDriver(
            command, request.cwd, emit, native_session_id=request.native_session_id
        )

    def auth_adapter(self, *, shell: str | None = None) -> object | None:
        if is_staging():
            return None
        command = _resolve_login_command("codex", shell=shell)
        if command is None:
            return None
        return CommandAuthAdapter(
            self.id,
            _command_with_args(command, "login", "status"),
            _command_with_args(command, "login", "--device-auth"),
        )


class AgyAdapter(_WorktreeAdapter, _StructuredAdapter):
    id = "agy"
    display_name = "Antigravity CLI (agy)"
    capabilities = HarnessCapabilities(
        launch_modes=frozenset({"structured"}),
        interrupt=True,
        native_resume=True,
        model_catalog=True,
        worktree=True,
        attachments=_IMAGE_TYPES,
        interactive_auth=True,
    )
    catalog_type = AgyCatalogAdapter
    catalog_suffix: tuple[str, ...] = ()

    def default_command(self) -> list[str]:
        return agy.default_command()

    def start(self, request: LaunchRequest, emit: EmitFn) -> agy.AgyDriver:
        command = list(request.command) if request.command else self.default_command()
        return agy.AgyDriver(
            agy.resume_command(command, request.native_session_id),
            request.cwd,
            emit,
            native_session_id=request.native_session_id,
        )

    def auth_adapter(self, *, shell: str | None = None) -> object | None:
        if is_staging():
            return None
        command = _resolve_login_command("agy", shell=shell)
        if command is None:
            return None
        return CommandAuthAdapter(
            self.id,
            _command_with_args(command, "--version"),
            list(command),
            requires_pty=True,
            sign_in="terminal",
        )


class DeepSeekAdapter(_WorktreeAdapter, _StructuredAdapter):
    id = "deepseek-harness"
    display_name = "DeepSeek Harness (local Web RPC)"
    capabilities = HarnessCapabilities(
        launch_modes=frozenset({"structured"}),
        interrupt=True,
        native_resume=True,
        model_catalog=True,
        worktree=True,
        attachments=_IMAGE_TYPES,
    )
    catalog_type = DeepSeekCatalogAdapter
    catalog_suffix: tuple[str, ...] = ()
    recover_after_restart = True

    def default_command(self) -> list[str]:
        return deepseek.default_command()

    def start(self, request: LaunchRequest, emit: EmitFn) -> deepseek.DeepSeekDriver:
        command = list(request.command) if request.command else self.default_command()
        return deepseek.DeepSeekDriver(
            command, request.cwd, emit, native_session_id=request.native_session_id
        )


BUILTIN_ADAPTERS = HarnessAdapterRegistry(
    [ClaudeCodeAdapter(), CodexAdapter(), AgyAdapter(), DeepSeekAdapter()]
)
