"""Adapter extensibility gate (drover#422).

A synthetic adapter with a capability mix no built-in harness has is
registered the way a new harness would be -- an adapter, a registry entry and
a host preset -- and driven through harnessd, the hub API, and the web and iOS
fixtures. No layer may need to know its ID: every decision comes from the
registry declaration or the envelope it publishes, and every undeclared
operation is refused before provider code runs.
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path

import pytest
from harness_fixture_adapter import (
    FIXTURE_DISPLAY_NAME,
    FIXTURE_ID,
    FixtureLabAdapter,
    fixture_preset,
)

from drover.schema import bootstrap
from drover.server.harness.adapters import (
    HarnessAdapter,
    HarnessAdapterRegistry,
    InvalidHarnessAdapter,
)
from drover.server.harness.auth import AuthFlowManager
from drover.server.harness.daemon import (
    DEFAULT_PRESETS,
    HarnessDaemonState,
    HarnessPreset,
    create_harness_server,
)
from drover.server.harness.pty import PtySessionManager
from drover.server.harness.registry import HarnessRegistry
from drover.server.harness.structured.adapters import (
    BUILTIN_ADAPTERS,
    ClaudeCodeAdapter,
    CodexAdapter,
)
from drover.server.metrics import MetricsCollector, start_metrics_server

ROOT = Path(__file__).resolve().parents[1]
WEB_FIXTURE = ROOT / "tests/fixtures/web/harness_capabilities_hosts.json"
IOS_FIXTURE = (
    ROOT / "apps/drover/DroverKit/Tests/DroverKitTests/Fixtures/"
    "harness-capabilities-mixed.json"
)
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="

# What harnessd publishes for the fixture. Written out by hand on purpose: the
# web and iOS fixtures below must carry exactly this row.
EXPECTED_ROW = {
    "name": FIXTURE_ID,
    "enabled": True,
    "display_name": FIXTURE_DISPLAY_NAME,
    "description": "Synthetic adapter (tests only)",
    "command": [],
    "capabilities": {
        "schema_version": 1,
        "harness_id": FIXTURE_ID,
        "launch_modes": ["pty", "structured"],
        "approvals": True,
        "interrupt": False,
        "native_resume": True,
        "model_catalog": False,
        "usage": False,
        "worktree": False,
        "interactive_auth": False,
        "turn_preferences": False,
        "attachments": ["image/png"],
    },
}


def _state(tmp_path: Path, adapter: FixtureLabAdapter) -> HarnessDaemonState:
    duckdb_path = tmp_path / "host.duckdb"
    bootstrap(parquet_dir=tmp_path / "host-parquet", duckdb_path=duckdb_path)
    # A provider whose adapter declares only `structured`, enabled on this
    # host, proves an enabled preset alone is not terminal-launchable.
    codex = HarnessPreset("codex", (sys.executable, "-c", "pass"), True, "Codex")
    return HarnessDaemonState(
        host_id="studio",
        display_name="Studio",
        kind="macos",
        registry=HarnessRegistry(duckdb_path),
        pty=PtySessionManager(),
        presets={
            **DEFAULT_PRESETS,
            "codex": codex,
            # Registration step 3 of 3: the host availability row.
            FIXTURE_ID: fixture_preset(
                (sys.executable, "-c", "import time; time.sleep(30)")
            ),
        },
        # Registration steps 1-2: the adapter and its registry entry.
        adapters=HarnessAdapterRegistry([*_builtins(), adapter]),
        auth=AuthFlowManager({}),
        worktrees_dir=tmp_path / "worktrees",
        attachments_dir=tmp_path / "attachments",
    )


def _builtins():
    return [
        BUILTIN_ADAPTERS.resolve(harness_id) for harness_id in BUILTIN_ADAPTERS.ids()
    ]


def _request(
    url: str, payload: dict | None = None, *, method: str | None = None
) -> tuple[int, dict]:
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


@pytest.fixture
def fleet(tmp_path):
    """harnessd with the fixture registered, behind a real hub."""
    adapter = FixtureLabAdapter()
    state = _state(tmp_path, adapter)
    host = create_harness_server(listen_host="127.0.0.1", listen_port=0, state=state)
    threading.Thread(target=host.serve_forever, daemon=True).start()
    host_url = "http://127.0.0.1:%d" % host.server_address[1]

    hub_path = tmp_path / "hub.duckdb"
    bootstrap(parquet_dir=tmp_path / "hub-parquet", duckdb_path=hub_path)
    HarnessRegistry(hub_path).register_host(
        host_id="studio",
        display_name="Studio",
        kind="macos",
        local_url=host_url,
        capabilities=state.capabilities(),
    )
    collector = MetricsCollector(
        duckdb_path=hub_path, incoming_dir=tmp_path / "incoming", summarizer_report={}
    )
    hub = start_metrics_server(host="127.0.0.1", port=0, collector=collector)
    hub_url = "http://127.0.0.1:%d" % hub.server_address[1]
    try:
        yield state, adapter, hub_url
    finally:
        for session_id in state.structured.session_ids():
            state.structured.close(session_id)
        state.pty.close_all()
        for server in (hub, host):
            server.shutdown()
            server.server_close()


def _launch(hub_url: str, payload: dict) -> tuple[int, dict]:
    return _request(f"{hub_url}/harness/hosts/studio/sessions", payload)


def _structured_session(hub_url: str, tmp_path: Path, **extra) -> str:
    status, created = _launch(
        hub_url,
        {"harness": FIXTURE_ID, "mode": "structured", "cwd": str(tmp_path), **extra},
    )
    assert status == 201, created
    return created["session_id"]


def test_fixture_is_test_only_and_registers_like_any_adapter():
    assert FIXTURE_ID not in BUILTIN_ADAPTERS.ids()
    assert FIXTURE_ID not in DEFAULT_PRESETS
    registry = HarnessAdapterRegistry([*_builtins(), FixtureLabAdapter()])
    assert registry.ids()[-1] == FIXTURE_ID
    # No built-in adapter shares its capability mix.
    declared = registry.resolve(FIXTURE_ID).capabilities
    assert all(
        BUILTIN_ADAPTERS.resolve(harness_id).capabilities != declared
        for harness_id in BUILTIN_ADAPTERS.ids()
    )


def test_host_envelope_publishes_the_fixture_from_its_declaration(tmp_path):
    rows = {
        row["name"]: row
        for row in _state(tmp_path, FixtureLabAdapter()).capabilities()["harnesses"]
    }
    assert rows[FIXTURE_ID] == EXPECTED_ROW
    # Built-in rows carry their adapter's display name too.
    assert rows["codex"]["display_name"] == CodexAdapter.display_name
    assert rows["claude-code"]["display_name"] == ClaudeCodeAdapter.display_name
    assert rows["shell"]["display_name"] == "Shell"


def test_hub_proxies_the_same_row_and_the_web_and_ios_fixtures_carry_it(fleet):
    _state, _adapter, hub_url = fleet
    status, listing = _request(f"{hub_url}/harness")
    assert status == 200
    host = next(item for item in listing["hosts"] if item["host_id"] == "studio")
    published = next(
        row for row in host["capabilities"]["harnesses"] if row["name"] == FIXTURE_ID
    )
    assert published == EXPECTED_ROW
    for fixture in (WEB_FIXTURE, IOS_FIXTURE):
        hosts = json.loads(fixture.read_text())["hosts"]
        rows = [
            row
            for item in hosts
            for row in item.get("capabilities", {}).get("harnesses", [])
            if isinstance(row, dict) and row.get("name") == FIXTURE_ID
        ]
        assert rows == [EXPECTED_ROW], fixture.name


def test_structured_launch_turns_and_approvals_run_without_name_branches(
    fleet, tmp_path
):
    state, adapter, hub_url = fleet
    session_id = _structured_session(
        hub_url,
        tmp_path,
        prompt="hello",
        images=[{"media_type": "image/png", "data_base64": PNG}],
    )
    # No worktree is declared, so the session runs in the requested directory.
    assert state.registry.get_session(session_id).cwd == str(tmp_path)
    assert not (tmp_path / "worktrees").exists()
    assert adapter.executions[0] == ("start", None)
    assert adapter.executions[1][0] == "attachments"
    assert adapter.executions[1][2] == ["image/png"]

    status, _ = _request(
        f"{hub_url}/harness/sessions/{session_id}/turns", {"text": "next"}
    )
    assert status == 202
    assert adapter.executions[-1] == ("turn", "next", None, None)

    status, _ = _request(
        f"{hub_url}/harness/sessions/{session_id}/permission",
        {"request_id": "req-1", "decision": "allow"},
    )
    assert status == 200
    assert adapter.executions[-1] == ("approval", "req-1", "allow")


def test_undeclared_operations_fail_before_provider_execution(fleet, tmp_path):
    state, adapter, hub_url = fleet
    session_id = _structured_session(hub_url, tmp_path)
    before = list(adapter.executions)

    refusals = {
        "interrupt": _request(
            f"{hub_url}/harness/sessions/{session_id}/interrupt", method="POST"
        ),
        "jpeg turn": _request(
            f"{hub_url}/harness/sessions/{session_id}/turns",
            {
                "text": "look",
                "images": [{"media_type": "image/jpeg", "data_base64": PNG}],
            },
        ),
        "model turn": _request(
            f"{hub_url}/harness/sessions/{session_id}/turns",
            {"text": "x", "model": "any"},
        ),
        "effort launch": _launch(
            hub_url,
            {
                "harness": FIXTURE_ID,
                "mode": "structured",
                "cwd": str(tmp_path),
                "thinking_effort": "high",
            },
        ),
        "resume without id": _launch(
            hub_url,
            {
                "harness": FIXTURE_ID,
                "mode": "structured",
                "cwd": str(tmp_path),
                "native_resume": {"latest": True},
            },
        ),
    }
    assert {key: status for key, (status, _) in refusals.items()} == {
        key: 400 for key in refusals
    }
    assert (
        refusals["interrupt"][1]["error"] == f"{FIXTURE_ID} does not support interrupt"
    )
    assert "image/jpeg" in refusals["jpeg turn"][1]["error"]
    assert "model_catalog" in refusals["model turn"][1]["error"]
    assert "model_catalog" in refusals["effort launch"][1]["error"]
    assert adapter.executions == before
    assert not list((tmp_path / "attachments").glob(f"{session_id}/*"))
    assert [row.session_id for row in state.registry.list_sessions()] == [session_id]


def test_pty_launch_needs_an_advertised_pty_mode(fleet, tmp_path):
    state, adapter, hub_url = fleet
    status, created = _launch(
        hub_url,
        {
            "harness": FIXTURE_ID,
            "mode": "pty",
            "cwd": str(tmp_path),
            "native_resume": {"session_id": "lab-native-1", "label": "lab work"},
        },
    )
    assert status == 201, created
    pty_session = state.pty.get(created["session_id"])
    # The adapter, not the daemon, turned the native session into arguments.
    assert list(pty_session.command)[-2:] == ["--resume", "lab-native-1"]
    row = state.registry.get_session(created["session_id"])
    assert (row.native_session_id, row.native_resume_label) == (
        "lab-native-1",
        "lab work",
    )
    # codex is enabled here, but only `structured` is advertised for it.
    status, refused = _launch(
        hub_url, {"harness": "codex", "mode": "pty", "cwd": str(tmp_path)}
    )
    assert status == 400
    assert refused["error"] == "codex does not support pty launch"
    # shell advertises pty but no native resume.
    status, refused = _launch(
        hub_url,
        {
            "harness": "shell",
            "cwd": str(tmp_path),
            "native_resume": {"session_id": "x"},
        },
    )
    assert status == 400
    assert refused["error"] == "shell does not support native_resume"


def test_native_resume_candidates_come_from_the_adapter_extension(fleet):
    _state, adapter, hub_url = fleet
    status, listing = _request(
        f"{hub_url}/harness/hosts/studio/native-sessions?harness={FIXTURE_ID}"
    )
    assert status == 200
    assert listing["sessions"] == [
        {
            "session_id": "lab-native-7",
            "label": "Lab work · lab-nati",
            "cwd": None,
            "native_resume": {"session_id": "lab-native-7", "label": "Lab work"},
            "harness": FIXTURE_ID,
        }
    ]
    # Shell does not advertise native_resume, so nothing is discovered for it.
    status, listing = _request(
        f"{hub_url}/harness/hosts/studio/native-sessions?harness=shell"
    )
    assert (status, listing["sessions"]) == (200, [])
    # Malformed adapter output is dropped rather than published.
    adapter.native_candidates.append({"session_id": "no-resume-object"})
    status, listing = _request(
        f"{hub_url}/harness/hosts/studio/native-sessions?harness={FIXTURE_ID}"
    )
    assert [item["session_id"] for item in listing["sessions"]] == ["lab-native-7"]


class NoResumeLab(FixtureLabAdapter):
    """The same fixture without native_resume (and so without discovery)."""

    capabilities = replace(FixtureLabAdapter.capabilities, native_resume=False)
    resume = HarnessAdapter.resume
    native_sessions = HarnessAdapter.native_sessions


def test_unadvertised_native_resume_refuses_before_the_driver(tmp_path):
    adapter = NoResumeLab()
    state = _state(tmp_path, adapter)
    row = next(r for r in state.capabilities()["harnesses"] if r["name"] == FIXTURE_ID)
    assert row["capabilities"]["native_resume"] is False
    host = create_harness_server(listen_host="127.0.0.1", listen_port=0, state=state)
    threading.Thread(target=host.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % host.server_address[1]
    try:
        for mode in ("structured", "pty"):
            status, refused = _request(
                f"{base}/sessions",
                {
                    "harness": FIXTURE_ID,
                    "mode": mode,
                    "cwd": str(tmp_path),
                    "native_resume": {"session_id": "lab-native-7"},
                },
            )
            assert status == 400, mode
            assert refused["error"] == f"{FIXTURE_ID} does not support native_resume"
        status, listing = _request(f"{base}/native-sessions?harness={FIXTURE_ID}")
        assert (status, listing["sessions"]) == (200, [])
        assert adapter.executions == []
        assert state.registry.list_sessions() == []
        assert state.pty.list_sessions() == []
    finally:
        state.pty.close_all()
        host.shutdown()
        host.server_close()


def test_discovery_extension_requires_the_native_resume_capability():
    class DiscoversWithoutResume(NoResumeLab):
        def native_sessions(self, *, home, cwd):
            return []

    with pytest.raises(InvalidHarnessAdapter, match="native_sessions"):
        HarnessAdapterRegistry([DiscoversWithoutResume()])


def test_continue_uses_the_target_matrix_for_handoff_and_native_resume(fleet, tmp_path):
    state, adapter, hub_url = fleet
    source = _structured_session(hub_url, tmp_path)

    status, resumed = _request(
        f"{hub_url}/harness/sessions/{source}/continue",
        {
            "target_host_id": "studio",
            "target_harness": FIXTURE_ID,
            "native_resume": {"session_id": "lab-native-2", "label": "lab"},
        },
    )
    assert status == 201, resumed
    assert resumed["mode"] == "structured"
    # Resumed through the adapter's own resume operation, with no seed turn.
    assert adapter.drivers[-1].request.native_session_id == "lab-native-2"
    assert adapter.executions[-1] == ("start", "lab-native-2")
    row = state.registry.get_session(resumed["session_id"])
    assert (row.native_session_id, row.handoff_mode, row.mode) == (
        "lab-native-2",
        "native_resume",
        "structured",
    )

    # A Drover handoff from another source becomes the first structured turn.
    other = _structured_session(hub_url, tmp_path)
    status, handed = _request(
        f"{hub_url}/harness/sessions/{other}/continue",
        {"target_host_id": "studio", "target_harness": FIXTURE_ID},
    )
    assert status == 201, handed
    assert handed["mode"] == "structured"
    assert adapter.executions[-2] == ("start", None)
    kind, text, *_ = adapter.executions[-1]
    assert kind == "turn"
    assert "Continue this Drover Harness session" in text
    assert state.registry.get_session(handed["session_id"]).handoff_mode == (
        "nexus_handoff"
    )

    status, refused = _request(
        f"{hub_url}/harness/sessions/{source}/continue",
        {
            "target_host_id": "studio",
            "target_harness": "shell",
            "native_resume": {"session_id": "x"},
        },
    )
    assert status == 400
    assert "native resume" in refused["error"]
