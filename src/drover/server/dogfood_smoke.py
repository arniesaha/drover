"""Read-only dogfood smoke checks for MCP handoff readiness."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, Callable

import click

from drover.config import default_config
from drover.server.mcp import tools as mcp_tools

DEFAULT_DUCKDB_PATH = default_config().duckdb_path

Check = dict[str, Any]
QualityCheck = Callable[..., dict[str, Any]]


def run_smoke(
    *,
    duckdb_path: Path,
    repo_owner: str,
    repo_name: str,
    branch: str | None = None,
    project_key: str | None = None,
    replay_session_id: str,
    since: str | None = None,
    quality_check: QualityCheck | None = None,
) -> dict[str, Any]:
    """Run MCP handoff smoke checks against an explicit DuckDB path.

    The runner only calls read-only MCP tool functions. Test setup may create
    fixture data, but this function never writes production or fixture state.
    """
    project = project_key or f"{repo_owner}/{repo_name}"
    checks = [
        _check_handoff(
            duckdb_path=duckdb_path,
            repo_owner=repo_owner,
            repo_name=repo_name,
            branch=branch,
        ),
        _check_session_replay(
            duckdb_path=duckdb_path,
            session_id=replay_session_id,
        ),
        _check_project_activity(
            duckdb_path=duckdb_path,
            project_key=project,
            since=since,
        ),
        _check_data_quality(
            duckdb_path=duckdb_path,
            project_key=project,
            quality_check=quality_check,
        ),
    ]
    failed = [check for check in checks if check["status"] in {"fail", "error"}]
    return {
        "status": "fail" if failed else "pass",
        "duckdb_path": str(duckdb_path),
        "repo": f"{repo_owner}/{repo_name}",
        "branch": branch,
        "project_key": project,
        "checks": checks,
    }


def render_report(report: dict[str, Any]) -> str:
    """Render a compact pass/fail report suitable for local smoke output."""
    status = report["status"].upper()
    lines = [
        f"{status} dogfood MCP smoke db={report['duckdb_path']} "
        f"project={report['project_key']}"
    ]
    for check in report["checks"]:
        dimensions = check.get("dimensions") or []
        suffix = f" [{', '.join(dimensions)}]" if dimensions else ""
        message = check.get("message") or ""
        lines.append(f"{check['status'].upper()} {check['name']}{suffix}: {message}")
    return "\n".join(lines)


def _check_handoff(
    *,
    duckdb_path: Path,
    repo_owner: str,
    repo_name: str,
    branch: str | None,
) -> Check:
    try:
        payload = mcp_tools.drover_handoff(
            duckdb_path=duckdb_path,
            repo_owner=repo_owner,
            repo_name=repo_name,
            branch=branch,
            max_summaries=5,
        )
    except Exception as exc:  # pragma: no cover - defensive CLI reporting
        return _error("handoff", exc)

    summaries = payload.get("summaries") or []
    active = payload.get("active_sessions") or []
    dimensions: list[str] = []
    if not summaries and not active:
        dimensions.append("handoff_availability")

    agents = {
        row.get("agent_id") for row in [*summaries, *active] if row.get("agent_id")
    }

    if dimensions:
        return _fail(
            "handoff",
            dimensions,
            (
                f"{len(summaries)} summaries, {len(active)} active sessions, "
                f"{len(agents)} distinct agents"
            ),
        )
    return _pass(
        "handoff",
        (
            f"{len(summaries)} summaries, {len(active)} active sessions, "
            f"{len(agents)} distinct agents"
        ),
    )


def _check_session_replay(*, duckdb_path: Path, session_id: str) -> Check:
    try:
        payload = mcp_tools.drover_session_replay(
            duckdb_path=duckdb_path,
            session_id=session_id,
            last_n_turns=10,
        )
    except Exception as exc:  # pragma: no cover - defensive CLI reporting
        return _error("session_replay", exc)

    events = payload.get("events") or []
    if not events:
        return _fail(
            "session_replay",
            ["replay_availability"],
            f"session {session_id!r} returned no replay events",
        )
    return _pass(
        "session_replay", f"session {session_id!r} returned {len(events)} events"
    )


def _check_project_activity(
    *,
    duckdb_path: Path,
    project_key: str,
    since: str | None,
) -> Check:
    try:
        payload = mcp_tools.drover_project_activity(
            duckdb_path=duckdb_path,
            project_key=project_key,
            since=since,
            limit=5,
        )
    except Exception as exc:  # pragma: no cover - defensive CLI reporting
        return _error("project_activity", exc)

    rows = payload.get("rows") or []
    dimensions: list[str] = []
    if not rows:
        dimensions.append("activity_availability")

    span_count = sum(int(row.get("span_count") or 0) for row in rows)
    if span_count <= 0:
        dimensions.append("span_freshness")

    if rows and not any(row.get("project_key") == project_key for row in rows):
        dimensions.append("project_attribution")

    if dimensions:
        return _fail(
            "project_activity",
            dimensions,
            f"{len(rows)} activity rows, {span_count} spans for {project_key}",
        )
    return _pass(
        "project_activity",
        f"{len(rows)} activity rows, {span_count} spans for {project_key}",
    )


def _check_data_quality(
    *,
    duckdb_path: Path,
    project_key: str,
    quality_check: QualityCheck | None,
) -> Check:
    tool = quality_check
    if tool is None:
        tool = getattr(mcp_tools, "drover_data_quality", None)
    if tool is None:
        return _skip(
            "data_quality",
            "drover_data_quality is not registered; optional until issue #82 lands",
        )

    try:
        kwargs: dict[str, Any] = {"duckdb_path": duckdb_path}
        params = inspect.signature(tool).parameters
        if "project_key" in params:
            kwargs["project_key"] = project_key
        payload = tool(**kwargs)
    except Exception as exc:  # pragma: no cover - defensive CLI reporting
        return _error("data_quality", exc)

    status = str(payload.get("status", "pass")).lower()
    if status in {"fail", "failed", "error"}:
        dimensions = payload.get("dimensions") or ["data_quality"]
        message = payload.get("message") or "data-quality check failed"
        return _fail("data_quality", list(dimensions), str(message))
    return _pass("data_quality", "data-quality check passed")


def _pass(name: str, message: str) -> Check:
    return {"name": name, "status": "pass", "dimensions": [], "message": message}


def _skip(name: str, message: str) -> Check:
    return {"name": name, "status": "skip", "dimensions": [], "message": message}


def _fail(name: str, dimensions: list[str], message: str) -> Check:
    return {
        "name": name,
        "status": "fail",
        "dimensions": dimensions,
        "message": message,
    }


def _error(name: str, exc: Exception) -> Check:
    return _fail(name, [f"{name}_error"], f"{type(exc).__name__}: {exc}")


@click.command("dogfood-smoke")
@click.option(
    "--db-path",
    type=click.Path(path_type=Path),
    default=DEFAULT_DUCKDB_PATH,
    show_default=True,
    help="DuckDB lakehouse path. Defaults to the live local Drover DB.",
)
@click.option(
    "--repo-owner", required=True, help="Repository owner for drover_handoff."
)
@click.option("--repo-name", required=True, help="Repository name for drover_handoff.")
@click.option(
    "--branch", default=None, help="Optional branch filter for drover_handoff."
)
@click.option(
    "--project-key",
    default=None,
    help="Project key for drover_project_activity. Defaults to owner/name.",
)
@click.option(
    "--replay-session-id",
    required=True,
    help="Session id to validate with drover_session_replay.",
)
@click.option(
    "--since",
    default=None,
    help="Optional ISO timestamp lower bound for drover_project_activity.",
)
def main(
    db_path: Path,
    repo_owner: str,
    repo_name: str,
    branch: str | None,
    project_key: str | None,
    replay_session_id: str,
    since: str | None,
) -> None:
    """Run the MCP dogfood handoff smoke check."""
    report = run_smoke(
        duckdb_path=db_path.expanduser(),
        repo_owner=repo_owner,
        repo_name=repo_name,
        branch=branch,
        project_key=project_key,
        replay_session_id=replay_session_id,
        since=since,
    )
    click.echo(render_report(report))
    if report["status"] != "pass":
        raise click.exceptions.Exit(1)


if __name__ == "__main__":
    main()
