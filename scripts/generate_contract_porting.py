#!/usr/bin/env python3
"""Generate the exhaustive source-to-contract accounting ledger."""

from __future__ import annotations

import re
from pathlib import Path


SOURCE = Path("tests/test_harness_daemon.py")
TARGET = Path("tests/contract/PORTING.md")
PORTED = {
    "test_harnessd_creates_shell_session": "test_shell_create_get_list_and_terminate",
    "test_harnessd_lists_and_gets_live_sessions": "test_shell_create_get_list_and_terminate",
    "test_a_repeated_handoff_create_returns_the_session_it_already_made": "test_handoff_source_is_idempotent_but_parent_is_not",
    "test_delegated_children_of_one_parent_are_separate_sessions": "test_handoff_source_is_idempotent_but_parent_is_not",
    "test_a_repeat_create_with_a_client_key_does_not_spawn_a_second_session": "test_client_session_key_is_idempotent",
    "test_creates_without_a_client_key_are_still_independent": "test_unkeyed_creates_are_distinct",
    "test_daemon_rejects_unauthenticated_when_token_set": "test_auth_and_top_level_error_envelopes",
    "test_daemon_accepts_bearer": "test_auth_and_top_level_error_envelopes",
    "test_daemon_healthz_open": "test_launcher_announces_bound_port_and_registers",
    "test_structured_interrupt_unknown_session_is_404": "test_unknown_session_errors_are_wire_stable",
    "test_empty_turn_still_rejected": "test_structured_launch_validation_contract",
    "test_structured_unknown_harness_rejected": "test_structured_launch_validation_contract",
    "test_structured_session_rejects_ask_permission_mode": "test_structured_launch_validation_contract",
    "test_structured_session_stores_default_permission_mode": "test_structured_lifecycle_and_event_push_contract",
    "test_structured_session_stores_explicit_auto_permission_mode": "test_structured_lifecycle_and_event_push_contract",
    "test_structured_session_rejects_unknown_permission_mode": "test_structured_launch_validation_contract",
}


def reason(name: str) -> str:
    if any(token in name for token in ("monkeypatch", "cache", "hung", "slow", "overlap", "lookup")):
        return "dropped: requires deterministic control of an internal dependency or timing window"
    if any(token in name for token in ("native_", "discovers_", "reads_", "resolve_")):
        return "dropped: unit-level filesystem/parser discovery has no daemon HTTP/WS operation"
    if any(token in name for token in ("worktree", "recovery", "restart", "staging", "registry")):
        return "dropped: requires pre-seeding or faulting durable daemon state before startup"
    if any(token in name for token in ("auth_start", "auth_flow", "typed_code", "sign_in")):
        return "dropped: requires a purpose-built provider login executable; no stable public fixture protocol yet"
    return "dropped: public behavior not yet represented; retain source test until an observable fixture is added"


def main() -> None:
    names = re.findall(r"^def (test_[^(]+)", SOURCE.read_text(), re.MULTILINE)
    lines = [
        "# Phase 1 contract porting ledger",
        "",
        "Every `tests/test_harness_daemon.py` test is accounted for below. Contract",
        "tests communicate solely with a launched daemon; dropped entries name the",
        "missing public seam rather than importing daemon internals.",
        "",
        "| Source test | Contract test / disposition |",
        "| --- | --- |",
    ]
    for name in names:
        result = PORTED.get(name)
        lines.append(f"| `{name}` | `{result}` |" if result else f"| `{name}` | {reason(name)} |")
    TARGET.write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
