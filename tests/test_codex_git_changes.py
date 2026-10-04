"""Shell-only Codex turns must leave durable, deterministic file evidence."""

from __future__ import annotations

import json
import subprocess
import sys
import threading

import pytest

from drover.server.harness.structured.codex import CodexDriver
from drover.server.harness.structured.git_changes import capture_head, changed_paths
from drover.server.memory_identity import project_control_event
from drover.server.summarizer.derive import compute_files_touched


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True)


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "user.email", "test@example.com")
    for name in ("edited.txt", "deleted.txt", "renamed.txt", "reverted.txt"):
        (tmp_path / name).write_text("original\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base")
    return tmp_path


def run_turn(repo, script):
    messages = []
    exited = threading.Event()

    def emit(message):
        messages.append(message)
        if "exited" in message.payload:
            exited.set()

    driver = CodexDriver([sys.executable, "-c", script], str(repo), emit)
    try:
        driver.start()
        driver.send_turn("edit through shell", "turn-1")
        assert exited.wait(10), "turn never exited"
    finally:
        driver.close()
    return messages


def projected_files(messages):
    return compute_files_touched(
        project_control_event(
            {
                "event_id": message.event_id,
                "session_id": "codex-shell-session",
                "event_type": message.type,
                "created_at": message.ts,
                "payload_json": json.dumps(message.to_payload()),
            },
            {"harness": "codex"},
        )
        for message in messages
    )


def test_shell_commits_and_working_tree_reach_memory_before_turn_complete(repo):
    messages = run_turn(
        repo,
        """
from pathlib import Path
import json, subprocess
def git(*args):
    subprocess.run(['git', *args], check=True, stdout=subprocess.DEVNULL)
Path('edited.txt').write_text('changed')
Path('deleted.txt').unlink()
git('mv', 'renamed.txt', 'new name.txt')
git('add', '-A')
git('commit', '-qm', 'shell changes')
Path('reverted.txt').write_text('temporary')
git('add', '-A')
git('commit', '-qm', 'temporary edit')
git('revert', '--no-edit', 'HEAD')
Path('staged.txt').write_text('staged')
git('add', 'staged.txt')
Path('untracked ü\\nfile.txt').write_text('untracked')
Path('edited.txt').write_text('unstaged')
print(json.dumps({'type': 'turn.completed', 'usage': {}}), flush=True)
""",
    )
    complete = next(i for i, m in enumerate(messages) if m.payload.get("turn_complete"))
    assert projected_files(messages[:complete]) == [
        "deleted.txt",
        "edited.txt",
        "new name.txt",
        "renamed.txt",
        "reverted.txt",
        "staged.txt",
        "untracked ü\nfile.txt",
    ]
    # The completion/exit boundary must not record the same snapshot twice.
    assert len([m for m in messages if m.payload.get("changes")]) == 1


def test_clean_committed_turn_is_not_empty(repo):
    messages = run_turn(
        repo,
        """
from pathlib import Path
import subprocess
Path('edited.txt').write_text('committed')
subprocess.run(['git', 'commit', '-qam', 'shell edit'], check=True)
""",
    )
    assert git(repo, "status", "--porcelain") == ""
    assert projected_files(messages) == ["edited.txt"]


def test_failed_turn_still_records_shell_edits(repo):
    messages = run_turn(
        repo,
        "from pathlib import Path; Path('edited.txt').write_text('changed'); exit(3)",
    )
    assert projected_files(messages) == ["edited.txt"]
    assert any(m.type == "error" for m in messages)


@pytest.mark.parametrize("initialized", [False, True])
def test_non_git_or_unchanged_workspace_does_not_invent_files(tmp_path, initialized):
    if initialized:
        git(tmp_path, "init", "-q")
    messages = run_turn(tmp_path, "print('no edits')")
    assert projected_files(messages) == []


def test_read_only_turn_does_not_include_earlier_commits(repo):
    assert projected_files(run_turn(repo, "print('no edits')")) == []


def test_subdirectory_paths_are_repo_relative_and_ignored_files_are_excluded(repo):
    git(repo, "config", "diff.relative", "true")
    (repo / ".gitignore").write_text("ignored.txt\n")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-qm", "ignore rule")
    subdir = repo / "nested"
    subdir.mkdir()
    messages = run_turn(
        subdir,
        """
from pathlib import Path
Path('../edited.txt').write_text('changed')
Path('../ignored.txt').write_text('ignored')
Path('../root new.txt').write_text('root')
Path('child.txt').write_text('child')
""",
    )
    assert projected_files(messages) == [
        "edited.txt",
        "nested/child.txt",
        "root new.txt",
    ]


def test_staged_edit_is_preserved_when_working_copy_matches_base(repo):
    messages = run_turn(
        repo,
        """
from pathlib import Path
import subprocess
Path('edited.txt').write_text('staged change')
subprocess.run(['git', 'add', 'edited.txt'], check=True)
Path('edited.txt').write_text('original\\n')
""",
    )
    assert projected_files(messages) == ["edited.txt"]


@pytest.mark.parametrize(
    "error", [OSError("missing git"), subprocess.TimeoutExpired("git", 5)]
)
def test_git_probe_failure_preserves_native_evidence(repo, monkeypatch, error):
    def unavailable(*args, **kwargs):
        raise error

    monkeypatch.setattr(subprocess, "run", unavailable)
    assert capture_head(str(repo)) is None
    assert changed_paths(str(repo), "a" * 40) == []
    messages = run_turn(
        repo,
        """
import json
print(json.dumps({'type': 'item.completed', 'item': {
    'type': 'file_change', 'changes': [{'path': 'native.txt'}]}}))
print(json.dumps({'type': 'turn.completed', 'usage': {}}))
""",
    )
    assert projected_files(messages) == ["native.txt"]
    assert any(m.payload.get("turn_complete") for m in messages)


def test_each_turn_captures_its_own_base_and_retains_previous_evidence(repo):
    messages = []
    exited = threading.Event()

    def emit(message):
        messages.append(message)
        if "exited" in message.payload:
            exited.set()

    driver = CodexDriver(
        [
            sys.executable,
            "-c",
            """
from pathlib import Path
import json, subprocess, sys
Path(sys.argv[-1]).write_text('committed')
subprocess.run(['git', 'add', '-A'], check=True)
subprocess.run(['git', 'commit', '-qm', 'edit'], check=True)
print(json.dumps({'type': 'thread.started', 'thread_id': 'thread-123'}))
print(json.dumps({'type': 'turn.completed', 'usage': {}}))
""",
        ],
        str(repo),
        emit,
    )
    try:
        for turn_id, path in [("turn-1", "first.txt"), ("turn-2", "second.txt")]:
            exited.clear()
            driver.send_turn(path, turn_id)
            assert exited.wait(10)
    finally:
        driver.close()
    evidence = [m for m in messages if m.payload.get("changes")]
    assert [m.turn_id for m in evidence] == ["turn-1", "turn-2"]
    assert evidence[0].payload["base_sha"] != evidence[1].payload["base_sha"]
    assert projected_files(evidence[:1]) == ["first.txt"]
    assert projected_files(evidence[1:]) == ["second.txt"]
    assert projected_files(messages) == ["first.txt", "second.txt"]
