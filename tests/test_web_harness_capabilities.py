"""Web console controls are derived from the capability envelope (#419).

The shared client module (static/harness_capabilities.js) is exercised under
node against fixtures for every built-in harness, a fixture adapter with an
unusual capability mix, malformed/future matrices and a legacy host.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from drover.server.harness.adapters import HarnessCapabilities
from drover.server.harness.capabilities import capability_matrix, validate_capabilities
from drover.server.harness.structured.adapters import BUILTIN_ADAPTERS
from drover.server.web.ui import load_page

STATIC = Path(__file__).resolve().parents[1] / "src/drover/server/web/static"
MODULE = STATIC / "harness_capabilities.js"
FIXTURE = Path(__file__).parent / "fixtures/web/harness_capabilities_hosts.json"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(
    NODE is None, reason="node is required for web JS tests"
)


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _host(host_id: str) -> dict:
    return next(h for h in _fixture()["hosts"] if h["host_id"] == host_id)


def _js(expressions: dict[str, str]) -> dict:
    """Evaluate expressions with C (module) and host(id) in scope."""
    script = f"""
const C = require({json.dumps(str(MODULE))});
const fixture = require({json.dumps(str(FIXTURE))});
const host = (id) => fixture.hosts.find((h) => h.host_id === id);
const pick = (o, keys) => Object.fromEntries(keys.map((k) => [k, o[k]]));
const attempt = (fn) => {{ try {{ fn(); return "ok"; }} catch (e) {{ return "error: " + e.message; }} }};
const exprs = {json.dumps(expressions)};
const out = {{}};
for (const [key, src] of Object.entries(exprs)) out[key] = eval(src);
process.stdout.write(JSON.stringify(out));
"""
    proc = subprocess.run(
        [NODE, "-e", script], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_fixture_matrices_match_the_adapter_registry():
    """Provider fixtures are what the registry publishes, not hand-guesses."""
    rows = {
        row["name"]: row["capabilities"]
        for row in _host("mac-mini")["capabilities"]["harnesses"]
    }
    for harness_id in BUILTIN_ADAPTERS.ids():
        adapter = BUILTIN_ADAPTERS.resolve(harness_id)
        assert rows[harness_id] == capability_matrix(harness_id, adapter.capabilities)
    assert rows["shell"] == capability_matrix(
        "shell", HarnessCapabilities(frozenset({"pty"}))
    )
    assert set(rows) == {*BUILTIN_ADAPTERS.ids(), "shell"}
    # The current and legacy hosts are valid central projections.
    for host_id in ("mac-mini", "old-nas"):
        host = _host(host_id)
        assert validate_capabilities(host["capabilities"], host_id)


@needs_node
def test_launch_targets_come_from_launch_modes_not_names():
    out = _js(
        {
            "targets": "C.launchTargets(host('mac-mini')).map((t) => [t.name, t.mode])",
            "preferred": "C.preferredLaunchTarget(host('mac-mini')).name",
            "fixture": "C.launchTargets(host('fixture-box')).map((t) => [t.name, t.mode, t.modes])",
        }
    )
    # Structured-capable first, then host order; shell is PTY-only.
    assert out["targets"] == [
        ["claude-code", "structured"],
        ["codex", "structured"],
        ["agy", "structured"],
        ["deepseek-harness", "structured"],
        ["shell", "pty"],
    ]
    assert out["preferred"] == "claude-code"
    # Structured is preferred when both are advertised; unknown modes ignored.
    assert out["fixture"] == [
        ["fixture-echo", "structured", ["structured", "pty"]],
        ["fixture-tty", "pty", ["pty"]],
    ]


@needs_node
def test_provider_controls_follow_their_matrices():
    keys = (
        "['approvals','interrupt','nativeResume','modelCatalog','worktree',"
        "'interactiveAuth','usage','attachments']"
    )
    out = _js(
        {
            name: f"pick(C.harnessControls(host('mac-mini'), '{name}'), {keys})"
            for name in ("claude-code", "codex", "agy", "deepseek-harness", "shell")
        }
    )
    images = ["image/gif", "image/jpeg", "image/png", "image/webp"]
    assert out["claude-code"] == {
        "approvals": True,
        "interrupt": True,
        "nativeResume": True,
        "modelCatalog": True,
        "worktree": False,
        "interactiveAuth": True,
        "usage": False,
        "attachments": images,
    }
    assert out["codex"]["approvals"] is False
    assert out["codex"]["worktree"] is True
    assert out["agy"] == {**out["codex"]}
    assert out["deepseek-harness"]["interactiveAuth"] is False
    assert out["shell"] == {
        "approvals": False,
        "interrupt": False,
        "nativeResume": False,
        "modelCatalog": False,
        "worktree": False,
        "interactiveAuth": False,
        "usage": False,
        "attachments": [],
    }


@needs_node
def test_unusual_fixture_adapter_fails_closed_on_what_it_does_not_understand():
    out = _js(
        {
            "echo": "C.harnessControls(host('fixture-box'), 'fixture-echo')",
            "pdf": "C.acceptsAttachment(C.harnessControls(host('fixture-box'), 'fixture-echo'), 'application/pdf')",
            "text": "C.acceptsAttachment(C.harnessControls(host('fixture-box'), 'fixture-echo'), 'text/markdown')",
            "png": "C.acceptsAttachment(C.harnessControls(host('fixture-box'), 'fixture-echo'), 'image/png')",
            "tty": "C.harnessControls(host('fixture-box'), 'fixture-tty')",
        }
    )
    echo = out["echo"]
    assert echo["launchable"] is True
    assert echo["approvals"] is True
    assert echo["interrupt"] is False
    assert echo["usage"] is True
    assert echo["interactiveAuth"] is False
    assert echo["attachments"] == ["application/pdf", "text/*"]
    assert "teleport" not in echo
    assert (out["pdf"], out["text"], out["png"]) == (True, True, False)
    # Optional v1 flags default to false; PTY-only harness may still declare
    # worktree.
    assert out["tty"]["mode"] == "pty"
    assert out["tty"]["worktree"] is True
    assert out["tty"]["approvals"] is False


@needs_node
def test_disabled_modeless_future_and_malformed_rows_offer_nothing():
    names = (
        "fixture-off",
        "fixture-modeless",
        "fixture-future",
        "fixture-null",
        "fixture-mismatch",
    )
    out = _js(
        {
            name: (
                f"pick(C.harnessControls(host('fixture-box'), '{name}'), "
                "['status','launchable','approvals','interrupt','modes'])"
            )
            for name in names
        }
    )
    assert out["fixture-off"]["status"] == "disabled"
    assert out["fixture-modeless"]["status"] == "no-mode"
    assert out["fixture-future"]["status"] == "unsupported"
    assert out["fixture-null"]["status"] == "invalid"
    assert out["fixture-mismatch"]["status"] == "invalid"
    for name in names:
        assert out[name]["launchable"] is False
    for name in ("fixture-future", "fixture-null", "fixture-mismatch"):
        assert out[name]["approvals"] is False
        assert out[name]["interrupt"] is False
        assert out[name]["modes"] == []


@needs_node
def test_legacy_host_is_metadata_only():
    out = _js(
        {
            "controls": "C.allControls(host('old-nas')).map((c) => [c.name, c.status, c.launchable, c.approvals])",
            "targets": "C.launchTargets(host('old-nas'))",
            "summary": "pick(C.hostSummary(host('old-nas')), ['legacy','launchable'])",
            "bare": "C.launchTargets(host('bare-host'))",
            "launch": "attempt(() => C.requireLaunch(host('old-nas'), 'claude-code'))",
        }
    )
    assert out["controls"] == [
        ["shell", "legacy", False, False],
        ["claude-code", "legacy", False, False],
        ["codex beta", "legacy", False, False],
    ]
    assert out["targets"] == []
    assert out["summary"] == {"legacy": True, "launchable": False}
    assert out["bare"] == []
    assert out["launch"].startswith("error: This host predates")


@needs_node
def test_observe_only_and_stale_selections_cannot_launch():
    out = _js(
        {
            "openclaw": "pick(C.harnessControls(host('mac-mini'), 'openclaw'), ['status','launchable'])",
            "openclaw_launch": "attempt(() => C.requireLaunch(host('mac-mini'), 'openclaw'))",
            "disabled_launch": "attempt(() => C.requireLaunch(host('fixture-box'), 'fixture-off'))",
            "wrong_host": "attempt(() => C.requireLaunch(host('fixture-box'), 'claude-code'))",
            "no_host": "attempt(() => C.requireLaunch(undefined, 'shell'))",
        }
    )
    assert out["openclaw"] == {"status": "missing", "launchable": False}
    assert out["openclaw_launch"].startswith("error:")
    assert out["disabled_launch"] == "error: Not enabled on this host."
    assert out["wrong_host"].startswith("error:")
    assert out["no_host"].startswith("error:")


@needs_node
def test_launch_body_carries_the_advertised_mode_and_only_supported_preferences():
    prefs = "{cwd: '/repo', rows: 40, cols: 120, model: 'm1', thinkingEffort: 'high'}"
    out = _js(
        {
            "claude": f"C.launchBody(C.requireLaunch(host('mac-mini'), 'claude-code'), {prefs})",
            "shell": f"C.launchBody(C.requireLaunch(host('mac-mini'), 'shell'), {prefs})",
            "echo": f"C.launchBody(C.requireLaunch(host('fixture-box'), 'fixture-echo'), {prefs})",
            "stale": "attempt(() => C.launchBody(C.harnessControls(host('fixture-box'), 'fixture-off'), {}))",
        }
    )
    assert out["claude"] == {
        "harness": "claude-code",
        "mode": "structured",
        "cwd": "/repo",
        "model": "m1",
        "thinking_effort": "high",
    }
    assert out["shell"] == {
        "harness": "shell",
        "mode": "pty",
        "cwd": "/repo",
        "rows": 40,
        "cols": 120,
    }
    # No model catalog advertised: a model choice is never sent.
    assert out["echo"] == {
        "harness": "fixture-echo",
        "mode": "structured",
        "cwd": "/repo",
    }
    assert out["stale"].startswith("error:")


@needs_node
def test_session_controls_require_a_structured_session_and_the_capability():
    def session(harness: str, mode: str | None) -> str:
        return json.dumps({"harness": harness, "mode": mode})

    keys = "['terminal','structured','turns','approvals','interrupt','attachments']"
    cases = {
        "claude": ("mac-mini", "claude-code", "structured"),
        "codex": ("mac-mini", "codex", "structured"),
        "shell": ("mac-mini", "shell", "pty"),
        "legacy_pty": ("old-nas", "shell", None),
        "legacy_structured": ("old-nas", "claude-code", "structured"),
        "pty_only_now": ("fixture-box", "fixture-tty", "structured"),
    }
    exprs = {
        key: f"pick(C.sessionControls(host('{h}'), {session(name, mode)}), {keys})"
        for key, (h, name, mode) in cases.items()
    }
    exprs["codex_approve"] = (
        "attempt(() => C.requireSessionAction(host('mac-mini'), "
        f"{session('codex', 'structured')}, 'approve'))"
    )
    exprs["legacy_turn"] = (
        "attempt(() => C.requireSessionAction(host('old-nas'), "
        f"{session('claude-code', 'structured')}, 'turns'))"
    )
    exprs["shell_interrupt"] = (
        "attempt(() => C.requireSessionAction(host('mac-mini'), "
        f"{session('shell', 'pty')}, 'interrupt'))"
    )
    exprs["claude_interrupt"] = (
        "attempt(() => C.requireSessionAction(host('mac-mini'), "
        f"{session('claude-code', 'structured')}, 'interrupt'))"
    )
    out = _js(exprs)
    assert out["claude"]["turns"] and out["claude"]["approvals"]
    assert out["claude"]["interrupt"] and out["claude"]["attachments"]
    assert out["claude"]["terminal"] is False
    assert out["codex"]["approvals"] is False and out["codex"]["interrupt"] is True
    assert out["shell"] == {
        "terminal": True,
        "structured": False,
        "turns": False,
        "approvals": False,
        "interrupt": False,
        "attachments": [],
    }
    # A pre-matrix PTY session is still a terminal (attach/Kill stay usable).
    assert out["legacy_pty"]["terminal"] is True
    for key in ("legacy_structured", "pty_only_now"):
        assert out[key]["turns"] is False
        assert out[key]["approvals"] is False
        assert out[key]["interrupt"] is False
    assert out["codex_approve"].startswith("error:")
    assert out["legacy_turn"].startswith("error: This host predates")
    assert out["shell_interrupt"].startswith("error:")
    assert out["claude_interrupt"] == "ok"


@needs_node
def test_pending_approval_is_the_newest_unanswered_prompt():
    prompt = lambda rid: {  # noqa: E731
        "event_type": "approval_prompt",
        "payload": {"request_id": rid, "tool": "Bash"},
        "content_preview": f"approval needed: {rid}",
    }
    answer = lambda rid: {  # noqa: E731
        "event_type": "approval_response",
        "payload": {"request_id": rid, "decision": "allow"},
    }
    out = _js(
        {
            "none": "C.pendingApproval([])",
            "open": f"C.pendingApproval({json.dumps([prompt('r1')])})",
            "answered": f"C.pendingApproval({json.dumps([prompt('r1'), answer('r1')])})",
            "second": f"C.pendingApproval({json.dumps([prompt('r1'), answer('r1'), prompt('r2')])})",
        }
    )
    assert out["none"] is None
    assert out["open"]["request_id"] == "r1"
    assert out["answered"] is None
    assert out["second"] == {
        "request_id": "r2",
        "tool": "Bash",
        "text": "approval needed: r2",
    }


HARNESS_NAMES = re.compile(
    r"claude|codex|\bagy\b|deepseek|openclaw|hermes|[\"']shell[\"']", re.IGNORECASE
)


@pytest.mark.parametrize(
    "name", ["harness.html", "harness_terminal.html", "harness_capabilities.js"]
)
def test_web_harness_code_has_no_harness_name_branches(name):
    source = (STATIC / name).read_text(encoding="utf-8")
    scripts = (
        source
        if name.endswith(".js")
        else "\n".join(re.findall(r"<script>(.*?)</script>", source, re.S))
    )
    assert not HARNESS_NAMES.findall(scripts)
    assert "HARNESS_PREFERENCE" not in scripts


@pytest.mark.parametrize("name", ["harness.html", "harness_terminal.html"])
def test_pages_inline_the_capability_module(name):
    page = load_page(name)
    assert "@include" not in page
    assert page.count("const DroverCapabilities = (() => {") == 1


@needs_node
@pytest.mark.parametrize("name", ["harness.html", "harness_terminal.html"])
def test_page_scripts_parse(name):
    for script in re.findall(r"<script>(.*?)</script>", load_page(name), re.S):
        proc = subprocess.run(
            [NODE, "-e", "new Function(require('fs').readFileSync(0, 'utf8'))"],
            input=script,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, proc.stderr


def test_pages_route_every_action_through_a_capability_guard():
    console = load_page("harness.html")
    assert (
        "DroverCapabilities.requireLaunch(hostById(host.host_id), harness)" in console
    )
    assert "DroverCapabilities.launchBody(controls" in console
    session = load_page("harness_terminal.html")
    for action in ("approve", "interrupt", "turns"):
        assert f'guardSessionAction("{action}")' in session
    assert (
        "DroverCapabilities.requireLaunch(hostById(targetHost), targetHarness)"
        in session
    )
    assert "!continueTarget().nativeResume" in session


@needs_node
def test_malformed_known_fields_close_the_entire_web_matrix():
    cases = {
        "approvals": '"yes"',
        "launch_modes": '["structured", 7]',
        "attachments": '["image/jpeg", "not-a-mime"]',
        "turn_preferences": "1",
    }
    out = _js(
        {
            key: "(() => { const h = JSON.parse(JSON.stringify(host('mac-mini'))); "
            + f"h.capabilities.harnesses[1].capabilities.{key} = {value}; "
            + "return C.harnessControls(h, 'claude-code'); })()"
            for key, value in cases.items()
        }
    )
    for controls in out.values():
        assert controls["status"] == "invalid"
        assert not controls["launchable"]
        assert not controls["approvals"]
        assert not controls["interrupt"]


def test_session_refresh_uses_new_host_matrix_instead_of_initial_fleet():
    page = load_page("harness_terminal.html")
    # Session responses include the current public host; the fleet loaded on
    # page entry is only for handoff targets and must not override it.
    assert "hostById(session.host_id) || sessionData?.host" not in page
    assert "hostById(session?.host_id) || sessionData?.host" not in page
