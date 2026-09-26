"""Contract tests for drive adapters; collection Sources use a separate boundary."""

from __future__ import annotations

from dataclasses import replace

import pytest

from drover.collect.sources import HermesSource
from drover.server.harness.adapters import (
    AdapterHealth,
    HarnessAdapter,
    HarnessAdapterRegistry,
    HarnessCapabilities,
    InvalidHarnessAdapter,
    LaunchRequest,
    UnsupportedHarnessOperation,
)
from drover.server.harness.structured.driver import StructuredMessage


class FixtureAdapter(HarnessAdapter):
    id = "fixture"
    display_name = "Fixture"
    capabilities = HarnessCapabilities(
        launch_modes=frozenset({"structured"}),
        approvals=True,
        interrupt=True,
        native_resume=False,
        model_catalog=False,
        usage=False,
        worktree=False,
        attachments=frozenset(),
        interactive_auth=False,
    )

    def default_command(self) -> list[str]:
        return ["fixture"]

    def start(self, request: LaunchRequest, emit):
        self.emit = emit
        self.request = request
        return self

    def send_turn(
        self,
        driver,
        text: str,
        turn_id: str,
        *,
        model: str | None = None,
        thinking_effort: str | None = None,
    ) -> None:
        self.turn_options = (model, thinking_effort)
        driver.emit(StructuredMessage("user_input", "user", text, turn_id=turn_id))

    def close(self, driver) -> None:
        self.closed = True

    def health(self) -> AdapterHealth:
        return AdapterHealth(available=True)

    def answer_permission(
        self, driver, request_id: str, decision: str, note: str | None
    ) -> None:
        driver.emit(
            StructuredMessage(
                "approval_response",
                "user",
                decision,
                payload={"request_id": request_id, "note": note},
            )
        )

    def interrupt(self, driver) -> None:
        self.interrupted = True


def test_fixture_resolves_and_preserves_structured_turn_id():
    registry = HarnessAdapterRegistry([FixtureAdapter()])
    adapter = registry.resolve("fixture", operation="structured")
    events: list[StructuredMessage] = []
    driver = adapter.start(LaunchRequest(cwd="/tmp/fixture"), events.append)

    adapter.send_turn(
        driver, "hello", "client-turn-1", model="fixture-model", thinking_effort="high"
    )
    assert events[0].to_payload()["turn_id"] == "client-turn-1"
    assert events[0].to_payload()["type"] == "user_input"
    assert adapter.turn_options == ("fixture-model", "high")
    assert registry.resolve("fixture", operation="approvals") is adapter
    adapter.answer_permission(driver, "permission-1", "allow", None)
    assert events[1].type == "approval_response"
    registry.resolve("fixture", operation="interrupt").interrupt(driver)
    assert adapter.interrupted


@pytest.mark.parametrize(
    "operation",
    [
        "pty",
        "native_resume",
        "model_catalog",
        "usage",
        "worktree",
        "attachments",
        "interactive_auth",
    ],
)
def test_unsupported_operations_are_rejected_before_dispatch(operation):
    registry = HarnessAdapterRegistry([FixtureAdapter()])
    with pytest.raises(UnsupportedHarnessOperation):
        registry.resolve("fixture", operation=operation)


def test_duplicate_id_is_rejected_without_replacing_first_adapter():
    first = FixtureAdapter()
    registry = HarnessAdapterRegistry([first])
    with pytest.raises(InvalidHarnessAdapter, match="duplicate"):
        registry.register(FixtureAdapter())
    assert registry.resolve("fixture") is first


@pytest.mark.parametrize(
    "capability,method",
    [
        ("approvals", "answer_permission"),
        ("interrupt", "interrupt"),
        ("native_resume", "resume"),
        ("model_catalog", "model_catalog_adapter"),
        ("usage", "usage"),
        ("worktree", "worktree_policy"),
        ("attachments", "send_attachments"),
        ("interactive_auth", "auth_adapter"),
    ],
)
def test_declared_operation_requires_implementation(capability, method):
    class Broken(FixtureAdapter):
        pass

    if capability == "attachments":
        Broken.capabilities = replace(
            FixtureAdapter.capabilities, attachments=frozenset({"image/png"})
        )
    else:
        Broken.capabilities = replace(FixtureAdapter.capabilities, **{capability: True})
    # Remove an inherited implementation when testing an already enabled hook.
    if method in {"answer_permission", "interrupt"}:
        setattr(Broken, method, getattr(HarnessAdapter, method))
    with pytest.raises(InvalidHarnessAdapter, match=method):
        HarnessAdapterRegistry([Broken()])


def test_invalid_registration_does_not_stop_other_adapters_or_sources(tmp_path):
    class Broken(FixtureAdapter):
        id = "broken"
        capabilities = replace(FixtureAdapter.capabilities, native_resume=True)

    registry = HarnessAdapterRegistry()
    errors = registry.register_all([Broken(), FixtureAdapter()])
    assert len(errors) == 1
    assert registry.resolve("fixture").id == "fixture"
    with pytest.raises(KeyError):
        registry.resolve("broken")
    source = HermesSource(tmp_path)
    assert source.id == "hermes"
    with pytest.raises(InvalidHarnessAdapter):
        registry.register(source)


def test_invalid_metadata_and_launch_mode_fail_closed():
    class BadId(FixtureAdapter):
        id = "Fixture With Spaces"

    class BadMode(FixtureAdapter):
        capabilities = replace(
            FixtureAdapter.capabilities, launch_modes=frozenset({"unknown"})
        )

    for adapter in (BadId(), BadMode()):
        with pytest.raises(InvalidHarnessAdapter):
            HarnessAdapterRegistry([adapter])


def test_missing_metadata_and_noncallable_hook_fail_closed():
    class MissingId(FixtureAdapter):
        id = None

    class BrokenInterrupt(FixtureAdapter):
        interrupt = None

    for adapter in (MissingId(), BrokenInterrupt()):
        with pytest.raises(InvalidHarnessAdapter):
            HarnessAdapterRegistry([adapter])


def test_undeclared_implementation_is_rejected():
    class HiddenResume(FixtureAdapter):
        def resume(self, request, emit):
            return self

    with pytest.raises(InvalidHarnessAdapter, match="resume"):
        HarnessAdapterRegistry([HiddenResume()])
