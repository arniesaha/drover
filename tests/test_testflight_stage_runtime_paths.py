"""Focused staging runtime-path tests that avoid importing server dependencies."""

import json
import subprocess
from pathlib import Path

import pytest
from testflight import stage

SHA = "a" * 40


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    root = tmp_path / "stage"
    repository = tmp_path / "repo"
    repository.mkdir()

    def run(argv, **kwargs):
        argv = [str(arg) for arg in argv]
        output = ""
        if "rev-parse" in argv:
            output = argv[-1].removesuffix("^{commit}") + "\n"
            if argv[-1] == "HEAD":
                output = Path(argv[argv.index("-C") + 1]).name + "\n"
        if "worktree" in argv:
            Path(argv[-2]).mkdir(parents=True)
        if "importlib.metadata" in " ".join(argv):
            output = "0.4.17\n"
        return subprocess.CompletedProcess(argv, 0, output, "")

    monkeypatch.setattr(stage.subprocess, "run", run)
    stage.prepare(repository, root, SHA, "https://stage.example.test")
    assert json.loads((root / "active-release.json").read_text())["source_sha"] == SHA
    return root, repository


def install_fake_codex(repository, monkeypatch):
    package = repository / "codex-install/lib/node_modules/@openai/codex"
    launcher = package / "bin/codex.js"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/usr/bin/env node\n")
    launcher.chmod(0o755)
    binary = package / "node_modules/@openai/codex-platform/vendor/bin/codex"
    binary.parent.mkdir(parents=True)
    binary.write_text("fixture\n")
    binary.chmod(0o755)
    executable_dir = repository / "codex-install/bin"
    executable_dir.mkdir(parents=True)
    (executable_dir / "codex").symlink_to(launcher)
    monkeypatch.setattr(stage, "SAFE_PATH", str(executable_dir))
    return binary


def test_release_accepts_codex_executable_aliases(runtime, monkeypatch):
    """Codex creates these aliases on every invocation in its isolated home."""
    root, repository = runtime
    binary = install_fake_codex(repository, monkeypatch)
    aliases = root / "home/.codex/tmp/arg0/codex-arg0fixture"
    aliases.mkdir(parents=True)
    (aliases / ".lock").touch()
    for name in ("applypatch", "apply_patch", "codex-execve-wrapper"):
        (aliases / name).symlink_to(binary)

    stage.release(root, SHA)


@pytest.mark.parametrize("unsafe", ["unexpected-name", "outside-package"])
def test_release_rejects_untrusted_codex_aliases(runtime, monkeypatch, unsafe):
    root, repository = runtime
    binary = install_fake_codex(repository, monkeypatch)
    aliases = root / "home/.codex/tmp/arg0/codex-arg0fixture"
    aliases.mkdir(parents=True)
    if unsafe == "unexpected-name":
        name = "credential-reader"
        target = binary
    else:
        name = "apply_patch"
        target = repository / "outside-codex"
        target.write_text("fixture\n")
        target.chmod(0o755)
    (aliases / name).symlink_to(target)

    with pytest.raises(stage.StageError):
        stage.release(root, SHA)
