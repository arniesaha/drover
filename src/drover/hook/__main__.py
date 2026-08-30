"""drover-hook — per-harness lifecycle hook CLI.

Invoked by Claude Code SessionStart/SessionEnd hooks (and per-harness
wrappers for OpenClaw, Hermes, pi-mono).

Per spec §3.1: hard 2-second budget. Two distinct failure modes:

- **Timeout** (server live but slow): prints
  ``"(drover timeout: context unavailable)"`` to stderr and exits 0.
- **Connection failure** (server unreachable/refused): prints
  ``"(drover offline)"`` to stderr and exits 0.

The hook never blocks the agent from starting.

Note: No automatic retry is performed. The hook operates within a
deliberate hard latency budget; retry would silently double it. Use
``--timeout`` to adjust the budget if needed.
"""

from __future__ import annotations

import logging
import os
import sys
import tomllib
from pathlib import Path
from typing import Optional

import click

from drover.config import default_config_file
from drover.hook.client import HookTimeout, call_tool
from drover.hook.context import detect_context
from drover.hook.render import render_handoff

log = logging.getLogger("drover.hook")

_DEFAULT_CONFIG_PATH = default_config_file("hook.toml")
_DEFAULT_MCP_URL = "http://127.0.0.1:7077/mcp"


def _load_hook_config(path: Optional[Path]) -> dict:
    p = path or _DEFAULT_CONFIG_PATH
    if not p.exists():
        return {}
    try:
        with open(p, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _emit_offline(reason: str) -> None:
    print("(drover offline)", file=sys.stderr)
    log.warning("drover-hook offline: %s", reason)


def _emit_timeout() -> None:
    """Emit the stable timeout sentinel. Never uses the word 'offline'."""
    print("(drover timeout: context unavailable)", file=sys.stderr)
    log.warning("drover-hook timeout: context unavailable")


@click.group()
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=None)
@click.pass_context
def main(ctx: click.Context, config_path: Optional[Path]) -> None:
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path
    logging.basicConfig(level=logging.WARNING, format="%(message)s")


@main.command(name="session-start")
@click.option(
    "--cwd", "cwd", type=click.Path(path_type=Path, exists=True), default=Path.cwd
)
@click.option("--mcp-url", default=None)
@click.option("--timeout", "timeout_s", type=float, default=2.0)
@click.option("--agent-id", default=None, help="Override agent_id from config")
@click.pass_context
def session_start(
    ctx: click.Context,
    cwd: Path,
    mcp_url: Optional[str],
    timeout_s: float,
    agent_id: Optional[str],
) -> None:
    """Resolve git context, fetch handoff, print to stdout."""
    cfg = _load_hook_config(ctx.obj["config_path"])
    if agent_id:
        cfg = {**cfg, "agent_id": agent_id}
    url = mcp_url or cfg.get("mcp_url") or _DEFAULT_MCP_URL

    context = detect_context(cwd=cwd, hook_config=cfg)
    if context.repo_owner is None and context.repo_name is None:
        # Non-git directory: still emit a benign banner so the agent knows drover saw nothing.
        click.echo(f"**Drover**: no git context for `{cwd}` — handoff skipped.")
        return

    try:
        payload = call_tool(
            mcp_url=url,
            tool="drover_handoff",
            args={
                "repo_owner": context.repo_owner,
                "repo_name": context.repo_name,
                "branch": context.branch,
            },
            timeout_s=timeout_s,
        )
    except HookTimeout:
        _emit_timeout()
        return
    except Exception as exc:  # noqa: BLE001
        _emit_offline(f"{type(exc).__name__}: {exc}")
        return

    click.echo(render_handoff(payload))


@main.command(name="session-end")
@click.option("--session-id", required=True)
@click.option("--mcp-url", default=None)
@click.option("--timeout", "timeout_s", type=float, default=2.0)
@click.pass_context
def session_end(
    ctx: click.Context,
    session_id: str,
    mcp_url: Optional[str],
    timeout_s: float,
) -> None:
    """Enqueue a summarize_jobs row by calling drover_session_close."""
    cfg = _load_hook_config(ctx.obj["config_path"])
    url = mcp_url or cfg.get("mcp_url") or _DEFAULT_MCP_URL

    try:
        call_tool(
            mcp_url=url,
            tool="drover_session_close",
            args={"session_id": session_id},
            timeout_s=timeout_s,
        )
    except HookTimeout:
        _emit_timeout()
    except Exception as exc:  # noqa: BLE001
        _emit_offline(f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":  # pragma: no cover
    main()
