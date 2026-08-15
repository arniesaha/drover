"""Sandbox workspace anchoring for structured harnesses.

Some harnesses derive a session's writable root from the working directory
they are launched with. DeepSeek Harness does exactly that: its own system
prompt reports ``file policy: workspace-write`` over "the session workspace",
and that workspace is the ``cwd`` handed to ``session.create``. Nothing widens
it afterwards -- a per-command approval ("allowed-once") authorizes one
command, not the workspace -- so the launch is the only moment at which the
root can still be got right.

Issue #183 is what that costs when it is wrong: a session was launched against
a directory that was not the checkout the work needed (the cwd was inherited
from a session another agent had configured, so no one chose it for this
work). Every write to the real checkout then sat outside the workspace, five
escalations were requested and granted one command at a time, and the session
could not stage or open a PR.

Hence two guarantees, both cheap enough for the launch path:

* a root the host cannot stat is refused before the native session exists,
  because an anchored session cannot be re-anchored;
* the root that was chosen is stated in the transcript, with a warning when it
  holds no git work tree -- the exact shape of the incident.

The helpers are harness-agnostic: any driver whose sandbox is anchored to its
cwd should call them rather than growing its own copy.
"""

from __future__ import annotations

import logging
import os
import subprocess
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from drover.server.harness.structured.driver import StructuredMessage

log = logging.getLogger("drover.harnessd")

# The probe runs before the user sees anything, so it is bounded twice over:
# git gets a short deadline, and the nearby-checkout scan gets a fixed budget
# of directory reads. A home directory full of projects costs a few hundred
# scandir entries, not a filesystem walk.
_GIT_TIMEOUT_SECONDS = 5.0
_SCAN_MAX_DEPTH = 2
_SCAN_DIR_BUDGET = 400
_SKIPPED_DIR_NAMES = frozenset(
    {
        "node_modules",
        "venv",
        "env",
        "target",
        "build",
        "dist",
        "vendor",
        "Library",
        "__pycache__",
        "site-packages",
    }
)


class WorkspaceAnchorError(RuntimeError):
    """The cwd cannot serve as a sandbox workspace root."""


@dataclass(frozen=True)
class WorkspaceAnchor:
    """What the sandbox workspace root turned out to be."""

    path: str
    is_work_tree: bool
    repo_root: str | None = None
    nearby_repo: str | None = None


def require_workspace_root(cwd: str, *, harness: str) -> str:
    """Return the expanded workspace root, or raise if it cannot be one.

    Raising is deliberate: the daemon turns a driver's start() exception into
    a 400 on ``/sessions`` with the message as its body (and marks the session
    ``errored``), so the launch fails loudly instead of producing a session
    anchored to a directory that does not exist.
    """
    path = Path(cwd).expanduser()
    display = str(path)
    try:
        is_dir = path.is_dir()
        exists = path.exists()
    except OSError as exc:  # unreadable mount, permission denied, ...
        raise WorkspaceAnchorError(
            f"{harness} cannot use {display} as its sandbox workspace root: {exc}"
        ) from exc
    if not exists:
        raise WorkspaceAnchorError(
            f"{harness} sandbox workspace root does not exist: {display}. "
            "The working directory becomes the session's only writable root, "
            "so it must be an existing directory. Relaunch with the checkout "
            "the work needs."
        )
    if not is_dir:
        raise WorkspaceAnchorError(
            f"{harness} sandbox workspace root is not a directory: {display}. "
            "The working directory becomes the session's only writable root. "
            "Relaunch with the checkout the work needs."
        )
    return display


def probe_workspace(path: str) -> WorkspaceAnchor:
    """Describe the workspace root: is it a checkout, and if not, is one near?"""
    repo_root = _git_toplevel(path)
    if repo_root is not None:
        return WorkspaceAnchor(path=path, is_work_tree=True, repo_root=repo_root)
    return WorkspaceAnchor(
        path=path,
        is_work_tree=False,
        nearby_repo=_nearby_repo(Path(path)),
    )


def workspace_messages(
    anchor: WorkspaceAnchor,
    *,
    harness: str,
    payload: dict | None = None,
) -> list[StructuredMessage]:
    """Status messages stating (and, when needed, warning about) the anchor."""
    shared = dict(payload or {})
    shared["sandbox_workspace"] = anchor.path
    shared["git_work_tree"] = anchor.is_work_tree
    messages = [
        StructuredMessage(
            type="status",
            role="system",
            text=(
                f"{harness} sandbox workspace: {anchor.path}. "
                "Writes outside it need a per-command approval, and the "
                "workspace cannot be widened once the session has started."
            ),
            payload={**shared, "repo_root": anchor.repo_root},
        )
    ]
    if anchor.is_work_tree:
        return messages
    if anchor.nearby_repo:
        advice = (
            f"A git checkout sits nearby at {anchor.nearby_repo}, outside the "
            "workspace. Working in it would need an approval per command; "
            "relaunch the session there instead."
        )
    else:
        advice = (
            "If this session needs a repository, relaunch it with that "
            "checkout as its working directory."
        )
    messages.append(
        StructuredMessage(
            type="status",
            role="system",
            text=f"{anchor.path} is not a git work tree. {advice}",
            payload={
                **shared,
                "workspace_warning": "not_a_git_work_tree",
                "nearby_repo": anchor.nearby_repo,
                "warning": True,
            },
        )
    )
    return messages


def _git_toplevel(path: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", path, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # No git, or a probe that outran its deadline. Unknown, not "absent":
        # claiming the cwd is not a checkout would be a false warning.
        log.debug("git rev-parse failed in %s: %s", path, exc)
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _nearby_repo(path: Path) -> str | None:
    """Find a checkout just below the cwd, or below its parent.

    Only "below" is searched: a checkout *above* the cwd would have made the
    cwd itself part of a work tree, which ``_git_toplevel`` already answers.
    The parent is included because that is the shape the incident took -- the
    checkout was one level down a sibling directory.
    """
    budget = _SCAN_DIR_BUDGET
    seen: set[Path] = set()
    candidates: list[Path] = []
    roots = [path]
    parent = path.parent
    if parent != path:
        roots.append(parent)
    for root in roots:
        queue: deque[tuple[Path, int]] = deque([(root, 0)])
        while queue and budget > 0:
            current, depth = queue.popleft()
            if current in seen:
                continue
            seen.add(current)
            budget -= 1
            try:
                entries = list(os.scandir(current))
            except OSError:
                continue
            for entry in entries:
                if entry.name == ".git" and current != path:
                    candidates.append(current)
                    break
            if depth >= _SCAN_MAX_DEPTH:
                continue
            for entry in entries:
                if entry.name.startswith(".") or entry.name in _SKIPPED_DIR_NAMES:
                    continue
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                queue.append((Path(entry.path), depth + 1))
    if not candidates:
        return None
    # A checkout whose directory name matches the one that was asked for is
    # almost certainly the one that was meant; otherwise take the shallowest.
    for candidate in candidates:
        if candidate.name == path.name:
            return str(candidate)
    return str(candidates[0])
