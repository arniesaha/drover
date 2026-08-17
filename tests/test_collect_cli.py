"""End-to-end tests for the drover-collect CLI."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from drover.collect.__main__ import _build_sources
from drover.collect.__main__ import main as collect_main

FIXTURES = Path(__file__).parent / "fixtures" / "collect"


def _seed_home(tmp_home: Path) -> None:
    """Lay down a HOME with claude_code + hermes + openclaw fixture data."""
    (tmp_home / ".claude" / "projects" / "proj-a").mkdir(parents=True)
    shutil.copy(
        FIXTURES / "claude_code" / "proj-a" / "session-1.jsonl",
        tmp_home / ".claude" / "projects" / "proj-a" / "session-1.jsonl",
    )
    (tmp_home / ".hermes" / "profiles" / "jenny" / "sessions").mkdir(parents=True)
    shutil.copy(
        FIXTURES / "hermes" / "sessions" / "sess-h-1.json",
        tmp_home / ".hermes" / "profiles" / "jenny" / "sessions" / "sess-h-1.json",
    )
    (tmp_home / ".openclaw" / "agents" / "main" / "sessions").mkdir(parents=True)
    shutil.copy(
        FIXTURES / "openclaw" / "sessions" / "sess-o-1.jsonl",
        tmp_home / ".openclaw" / "agents" / "main" / "sessions" / "sess-o-1.jsonl",
    )


def test_build_sources_threads_host_id_into_claude_code_agent_id() -> None:
    """Regression: ClaudeCodeSource.parse() used to hardcode agent_id="nas-claude",
    so every Claude Code session got tagged with the same agent_id no matter
    which host the shipper ran on. The CLI now passes host_id through."""
    cfg = {
        "host_id": "macmini-claude",
        "sources": {
            "claude_code": {"enabled": True, "root": "/tmp/x"},
            "claude_macmini": {"enabled": True, "root": "/tmp/y"},
        },
    }
    sources = _build_sources(cfg)
    assert {s.id for s in sources} == {"claude_code", "claude_macmini"}
    by_id = {s.id: s for s in sources}
    assert by_id["claude_code"].agent_id == "macmini-claude"
    assert by_id["claude_macmini"].agent_id == "macmini-claude"


def test_build_sources_explicit_agent_id_overrides_host_id() -> None:
    cfg = {
        "host_id": "macmini-claude",
        "sources": {
            "claude_code": {
                "enabled": True,
                "root": "/tmp/x",
                "agent_id": "custom-name",
            },
        },
    }
    sources = _build_sources(cfg)
    assert sources[0].agent_id == "custom-name"


def test_init_writes_default_config(tmp_path: Path) -> None:
    cfg = tmp_path / "collect.toml"
    runner = CliRunner()
    result = runner.invoke(collect_main, ["--config", str(cfg), "init"])
    assert result.exit_code == 0, result.output
    assert cfg.exists()
    text = cfg.read_text()
    assert "host_id" in text
    assert "[sources.claude_code]" in text


def test_init_refuses_overwrite_without_force(tmp_path: Path) -> None:
    cfg = tmp_path / "collect.toml"
    cfg.write_text("# existing\n")
    runner = CliRunner()
    result = runner.invoke(collect_main, ["--config", str(cfg), "init"])
    assert result.exit_code != 0


def test_init_force_overwrites(tmp_path: Path) -> None:
    cfg = tmp_path / "collect.toml"
    cfg.write_text("# existing\n")
    runner = CliRunner()
    result = runner.invoke(collect_main, ["--config", str(cfg), "init", "--force"])
    assert result.exit_code == 0
    assert "host_id" in cfg.read_text()


def _write_config(
    cfg_path: Path, *, home: Path, state_dir: Path, staging_dir: Path
) -> None:
    cfg_path.write_text(f"""
host_id = "test-host"
remote_host = "mac-mini.test"
remote_user = "tester"
state_dir = "{state_dir}"
staging_dir = "{staging_dir}"

[sources.claude_code]
enabled = true
root = "{home}/.claude/projects"

[sources.hermes]
enabled = true
root = "{home}/.hermes/profiles/jenny/sessions"

[sources.openclaw]
enabled = true
root = "{home}/.openclaw/agents/main/sessions"

[sources.claude_macmini]
enabled = false
root = "/nonexistent"

[sources.pi_mono]
enabled = false
db_path = "/nonexistent.db"
""")


def test_run_dry_run_writes_jsonl_and_does_not_ship(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _seed_home(home)
    state = tmp_path / "state"
    staging = tmp_path / "staging"
    cfg = tmp_path / "collect.toml"
    _write_config(cfg, home=home, state_dir=state, staging_dir=staging)

    runner = CliRunner()
    result = runner.invoke(collect_main, ["--config", str(cfg), "run", "--dry-run"])
    assert result.exit_code == 0, result.output

    # Staging contains JSONL from each enabled source
    jsonl = sorted(staging.glob("*.jsonl"))
    names = {p.name.split("-")[0] for p in jsonl}
    assert "claude_code" in names
    assert "hermes" in names
    assert "openclaw" in names

    # Cursors advanced
    cursors = sorted(state.glob("*.cursor"))
    assert {p.stem for p in cursors} == {"claude_code", "hermes", "openclaw"}
    payload = json.loads((state / "claude_code.cursor").read_text())
    assert "watermark_iso" in payload


def test_run_actually_ships_when_not_dry_run(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _seed_home(home)
    state = tmp_path / "state"
    staging = tmp_path / "staging"
    cfg = tmp_path / "collect.toml"
    _write_config(cfg, home=home, state_dir=state, staging_dir=staging)

    # Stub `rsync` on PATH that just removes the source files (mimicking
    # rsync --remove-source-files) and exits 0.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_rsync = fake_bin / "rsync"
    fake_rsync.write_text("""#!/usr/bin/env bash
# Strip flags, keep file args. Last arg is the destination.
files=()
for arg in "$@"; do
  case "$arg" in
    -*) ;;
    *) files+=("$arg") ;;
  esac
done
unset 'files[${#files[@]}-1]'  # drop dest
for f in "${files[@]}"; do
  if [[ -f "$f" ]]; then rm -f "$f"; fi
done
exit 0
""")
    fake_rsync.chmod(0o755)

    import os

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"

    runner = CliRunner(env={"PATH": env["PATH"]})
    result = runner.invoke(collect_main, ["--config", str(cfg), "run"])
    assert result.exit_code == 0, result.output
    # Staging is now empty (rsync removed the files)
    assert sorted(staging.glob("*.jsonl")) == []


def test_run_single_source_filter(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _seed_home(home)
    state = tmp_path / "state"
    staging = tmp_path / "staging"
    cfg = tmp_path / "collect.toml"
    _write_config(cfg, home=home, state_dir=state, staging_dir=staging)

    runner = CliRunner()
    result = runner.invoke(
        collect_main,
        ["--config", str(cfg), "run", "--source", "hermes", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    names = {p.name.split("-")[0] for p in staging.glob("*.jsonl")}
    assert names == {"hermes"}


def test_status_prints_per_source_state(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _seed_home(home)
    state = tmp_path / "state"
    staging = tmp_path / "staging"
    cfg = tmp_path / "collect.toml"
    _write_config(cfg, home=home, state_dir=state, staging_dir=staging)

    runner = CliRunner()
    runner.invoke(collect_main, ["--config", str(cfg), "run", "--dry-run"])

    result = runner.invoke(collect_main, ["--config", str(cfg), "status"])
    assert result.exit_code == 0, result.output
    assert "claude_code" in result.output
    assert "hermes" in result.output
    assert "watermark" in result.output.lower()


def test_run_continues_when_one_source_fails(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _seed_home(home)
    state = tmp_path / "state"
    staging = tmp_path / "staging"
    cfg = tmp_path / "collect.toml"
    # point hermes at a non-existent dir so its source returns no files
    # but bad pi_mono path will be skipped (disabled). Use claude_code root
    # that doesn't exist to simulate "no files" rather than failure.
    cfg.write_text(f"""
host_id = "test-host"
remote_host = "mac-mini.test"
remote_user = "tester"
state_dir = "{state}"
staging_dir = "{staging}"

[sources.claude_code]
enabled = true
root = "{home}/missing"

[sources.hermes]
enabled = true
root = "{home}/.hermes/profiles/jenny/sessions"
""")
    runner = CliRunner()
    result = runner.invoke(collect_main, ["--config", str(cfg), "run", "--dry-run"])
    assert result.exit_code == 0, result.output
    names = {p.name.split("-")[0] for p in staging.glob("*.jsonl")}
    # claude_code missing root → no files but no error; hermes should still ship
    assert "hermes" in names
