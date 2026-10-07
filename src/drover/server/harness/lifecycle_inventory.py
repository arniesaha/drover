"""Read-only Git inventory. No cleanup, archive or ref-writing commands."""

from __future__ import annotations

import subprocess
from pathlib import Path

from drover.server.harness.models import TERMINAL_SESSION_STATUSES


def _git(path, *args):
    return subprocess.run(
        ["git", "--no-optional-locks", "-C", str(path), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=5,
    ).stdout


def worktree_inventory(sessions, worktrees=None, home=None):
    root = (Path.home() if home is None else Path(home)) / ".drover" / "worktrees"
    worktrees = worktrees or {}
    sessions = list(sessions)
    by_path = {}
    repos = set()
    for session in sessions:
        if session.cwd:
            path = Path(session.cwd)
            by_path.setdefault(str(path.resolve()), []).append(session)
            if path.is_dir():
                repos.add(path)
    for sid, tree in worktrees.items():
        repos.add(Path(tree.repo_root))
        session = next((s for s in sessions if s.session_id == sid), None)
        if session:
            by_path.setdefault(str(Path(tree.path).resolve()), []).append(session)
    # Include preserved canonical trees whose original cwd no longer exists.
    if root.is_dir():
        repos.update(p for p in root.iterdir() if p.is_dir())
    entries = {}
    errors = []
    seen_repos = set()
    for repo in sorted(repos):
        try:
            common = (
                _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
                .decode()
                .strip()
            )
            if common in seen_repos:
                continue
            seen_repos.add(common)
            raw = _git(repo, "worktree", "list", "--porcelain", "-z")
            for block in raw.split(b"\0\0"):
                fields = {}
                for item in block.split(b"\0"):
                    if item:
                        key, _, value = item.partition(b" ")
                        fields[key.decode()] = value.decode(errors="surrogateescape")
                if "worktree" not in fields:
                    continue
                path = Path(fields["worktree"])
                candidates = {
                    s.session_id: s for s in by_path.get(str(path.resolve()), [])
                }
                session = (
                    next(iter(candidates.values())) if len(candidates) == 1 else None
                )
                canonical = (
                    path.is_absolute()
                    and path.parent == root
                    and path.resolve() == path
                    and root.resolve() == root
                )
                known_tree = worktrees.get(session.session_id) if session else None
                matching_path = session is not None and (
                    (session.cwd and Path(session.cwd) == path)
                    or (known_tree and Path(known_tree.path) == path)
                )
                owned = bool(canonical and matching_path)
                reasons = []
                if not owned:
                    reasons.append("foreign_or_ambiguous_ownership")
                if session and session.status not in TERMINAL_SESSION_STATUSES:
                    reasons.append("live_session")
                try:
                    dirty = bool(_git(path, "status", "--porcelain", "--ignored", "-z"))
                    if dirty:
                        reasons.append("local_changes")
                except (OSError, subprocess.SubprocessError):
                    dirty = None
                    reasons.append("inspection_failed")
                tree = worktrees.get(session.session_id) if session else None
                base = tree.base_sha if tree else None
                head = fields.get("HEAD")
                # A clean tree is not evidence that its contribution landed.
                if owned and not reasons:
                    if base and head == base:
                        reasons.append("untouched_candidate_requires_fresh_checks")
                    else:
                        reasons.append("unfinished_or_unknown_contribution")
                entries[str(path)] = {
                    "path": str(path),
                    "repo": common,
                    "session_id": session.session_id if session else None,
                    "branch": fields.get("branch"),
                    "base_sha": base,
                    "observed_head": head,
                    "ownership": "owned" if owned else "foreign",
                    "dirty": dirty,
                    "reasons": reasons,
                    "would_collect": owned
                    and reasons == ["untouched_candidate_requires_fresh_checks"],
                }
        except (OSError, subprocess.SubprocessError, ValueError):
            errors.append({"repo": str(repo), "reason": "inspection_failed"})
    return {"worktrees": list(entries.values()), "errors": errors}
