"""A synthetic drive adapter for the adapter-extensibility gate (drover#422).

Test-only: it is never added to ``BUILTIN_ADAPTERS`` and never ships as a
user-visible harness. Its capability mix is deliberately unlike every built-in
adapter, so any layer that still keys behavior on a harness name instead of
the advertised matrix shows up as a failure:

- both ``structured`` and ``pty`` launch modes (no built-in offers both);
- approvals without interrupt (Claude Code has both, the rest neither);
- native resume without restart recovery;
- no model catalog, so no model or thinking-effort controls;
- no worktree isolation;
- PNG attachments only.

Every provider-side call is recorded in ``executions`` so tests can prove an
unsupported operation was refused before it reached the provider.
"""

from __future__ import annotations

from typing import Any

from drover.server.harness.adapters import (
    AdapterHealth,
    HarnessAdapter,
    HarnessCapabilities,
    LaunchRequest,
)
from drover.server.harness.daemon import HarnessPreset
from drover.server.harness.structured.driver import EmitFn

FIXTURE_ID = "synthetic-lab"
FIXTURE_DISPLAY_NAME = "Synthetic Lab"


class FixtureLabDriver:
    """In-process driver: no subprocess, nothing to clean up."""

    def __init__(self, request: LaunchRequest, emit: EmitFn) -> None:
        self.request = request
        self.emit = emit
        self.alive = False

    def start(self) -> None:
        self.alive = True

    def is_alive(self) -> bool:
        return self.alive

    def has_turn_in_flight(self) -> bool:
        return False

    def close(self) -> None:
        self.alive = False


class FixtureLabAdapter(HarnessAdapter):
    id = FIXTURE_ID
    display_name = FIXTURE_DISPLAY_NAME
    capabilities = HarnessCapabilities(
        launch_modes=frozenset({"structured", "pty"}),
        approvals=True,
        native_resume=True,
        attachments=frozenset({"image/png"}),
    )

    def __init__(self) -> None:
        self.drivers: list[FixtureLabDriver] = []
        self.executions: list[tuple[Any, ...]] = []

    def default_command(self) -> list[str]:
        return [FIXTURE_ID, "--stdio"]

    def build_command(self, request: LaunchRequest) -> list[str]:
        command = list(request.command or self.default_command())
        if request.native_session_id:
            command += ["--resume", request.native_session_id]
        return command

    def start(self, request: LaunchRequest, emit: EmitFn) -> FixtureLabDriver:
        self.executions.append(("start", request.native_session_id))
        driver = FixtureLabDriver(request, emit)
        self.drivers.append(driver)
        return driver

    def resume(self, request: LaunchRequest, emit: EmitFn) -> FixtureLabDriver:
        return self.start(request, emit)

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
        self.executions.append(("turn", text, model, thinking_effort))

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
        self.executions.append(
            ("attachments", text, [item["media_type"] for item in attachments])
        )

    def answer_permission(
        self, driver: Any, request_id: str, decision: str, note: str | None
    ) -> None:
        self.executions.append(("approval", request_id, decision))

    def close(self, driver: Any) -> None:
        driver.close()

    def health(self) -> AdapterHealth:
        return AdapterHealth(available=True)


def fixture_preset(command: tuple[str, ...] = (FIXTURE_ID,)) -> HarnessPreset:
    """The host availability row a real install would resolve at startup."""
    return HarnessPreset(
        name=FIXTURE_ID,
        command=command,
        enabled=True,
        description="Synthetic adapter (tests only)",
    )
