"""Detect agent + git context for the per-harness hook.

A null git context (no remote / no branch) is acceptable — the
caller still gets an AgentContext and can decide whether to call
drover_handoff with task_id-style override or just skip.
"""

from __future__ import annotations

import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from drover.task_id import parse_repo_url


@dataclass(frozen=True)
class AgentContext:
    agent_id: str
    repo_owner: Optional[str]
    repo_name: Optional[str]
    branch: Optional[str]


def _git(cwd: Path, *args: str) -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def _resolve_remote(cwd: Path) -> tuple[Optional[str], Optional[str]]:
    url = _git(cwd, "remote", "get-url", "origin")
    if not url:
        return None, None
    return parse_repo_url(url)


def _default_agent_id() -> str:
    host = socket.gethostname().split(".")[0] or "unknown"
    return f"{host}-claude"


def detect_context(*, cwd: Path, hook_config: Optional[dict] = None) -> AgentContext:
    cfg = hook_config or {}
    agent_id = cfg.get("agent_id") or _default_agent_id()

    repo_owner, repo_name = _resolve_remote(cwd)
    branch = _git(cwd, "symbolic-ref", "--short", "HEAD")

    return AgentContext(
        agent_id=agent_id,
        repo_owner=repo_owner,
        repo_name=repo_name,
        branch=branch,
    )
