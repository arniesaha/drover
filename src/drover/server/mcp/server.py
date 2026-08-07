"""FastMCP wrapper that registers the Drover MCP tools.

The MCP surface is a thin proxy over functions in ``tools.py``. Each
tool function takes a ``duckdb_path`` keyword arg; the wrapper closes
over the configured path so callers don't need to pass it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

from drover.server.mcp import tools as t
from drover.server.summarizer.backends import SummarizerBackendConfig


def build_mcp_server(
    *,
    duckdb_path: Path,
    name: str = "drover",
    host: str = "0.0.0.0",
    port: int = 7077,
    backend_config: Optional[SummarizerBackendConfig] = None,
    summarize_job_stream: object | None = None,
) -> FastMCP:
    """Construct a FastMCP server with all Drover tools registered.

    ``backend_config`` is optional — it's only needed by tools that call
    out to an LLM on demand (currently ``drover_active_handoff``). If
    unset, those tools will raise at call-time.
    """
    mcp = FastMCP(name, host=host, port=port)
    db = Path(duckdb_path)
    bcfg = backend_config

    @mcp.tool()
    def drover_handoff(
        repo_owner: Optional[str] = None,
        repo_name: Optional[str] = None,
        branch: Optional[str] = None,
        task_id: Optional[str] = None,
        max_summaries: int = 3,
    ) -> dict:
        """Return recent session summaries and currently-active sessions for a
        task, identified by either ``task_id`` or ``(repo_owner, repo_name, branch)``.
        """
        return t.drover_handoff(
            duckdb_path=db,
            repo_owner=repo_owner,
            repo_name=repo_name,
            branch=branch,
            task_id=task_id,
            max_summaries=max_summaries,
        )

    @mcp.tool()
    def drover_session_replay(
        session_id: str, last_n_turns: int = 30, include_empty: bool = False
    ) -> dict:
        """Return the most recent substantive agent_events for one session, newest first.

        Empty metadata-only events are hidden by default. Pass include_empty=true
        when debugging raw ingestion.
        """
        return t.drover_session_replay(
            duckdb_path=db,
            session_id=session_id,
            last_n_turns=last_n_turns,
            include_empty=include_empty,
        )

    @mcp.tool()
    def drover_session_summary(session_id: str) -> Optional[dict]:
        """Return the session_summaries row for one session, or null if no summary exists."""
        return t.drover_session_summary(duckdb_path=db, session_id=session_id)

    @mcp.tool()
    def drover_active_sessions(task_id: Optional[str] = None) -> dict:
        """List currently-active sessions (no summary, event within last 30 min)."""
        return t.drover_active_sessions(duckdb_path=db, task_id=task_id)

    @mcp.tool()
    def drover_search(
        query: str,
        task_id: Optional[str] = None,
        repo: Optional[str] = None,
        since: Optional[str] = None,
        limit: int = 50,
        default_since_days: int = 30,
    ) -> dict:
        """Case-insensitive content search across agent_events.

        ``repo`` matches the literal ``<owner>/<name>`` (e.g. ``arniesaha/drover``).
        ``since`` is an ISO-8601 timestamp lower bound. Unscoped searches default
        to the last ``default_since_days`` days to keep live MCP recall bounded.
        """
        return t.drover_search(
            duckdb_path=db,
            query=query,
            task_id=task_id,
            repo=repo,
            since=since,
            limit=limit,
            default_since_days=default_since_days,
        )

    @mcp.tool()
    def drover_files_touched(task_id: str, since: Optional[str] = None) -> dict:
        """Return distinct file paths edited under ``task_id``, derived from
        tool_use_blocks (Edit/Write inputs)."""
        return t.drover_files_touched(duckdb_path=db, task_id=task_id, since=since)

    @mcp.tool()
    def drover_session_close(session_id: str) -> dict:
        """Enqueue a source-versioned summary generation for the session."""
        return t.drover_session_close(
            duckdb_path=db,
            session_id=session_id,
            summarize_job_stream=summarize_job_stream,
        )

    @mcp.tool()
    def drover_project_brief(
        repo_owner: Optional[str] = None,
        repo_name: Optional[str] = None,
        project_key: Optional[str] = None,
    ) -> Optional[dict]:
        """Return the latest project-level brief for a repository (what is this
        project, current state, recent themes, key files, open questions). Returns
        null if no brief has been generated yet — the brief worker generates one
        whenever a session in that repo is summarized."""
        return t.drover_project_brief(
            duckdb_path=db,
            repo_owner=repo_owner,
            repo_name=repo_name,
            project_key=project_key,
        )

    @mcp.tool()
    def drover_recent_sessions(
        repo_owner: Optional[str] = None,
        repo_name: Optional[str] = None,
        project_key: Optional[str] = None,
        limit: int = 5,
    ) -> dict:
        """Return the most recent session summaries for a repository.

        Strictly more fine-grained than drover_project_brief — use this when you
        want the actual last-N session narratives instead of a synthesis."""
        return t.drover_recent_sessions(
            duckdb_path=db,
            repo_owner=repo_owner,
            repo_name=repo_name,
            project_key=project_key,
            limit=limit,
        )

    @mcp.tool()
    def drover_recent_contexts(
        container_type: Optional[str] = None,
        source_harness: Optional[str] = None,
        limit: int = 10,
    ) -> dict:
        """Return recent confidence-aware context containers beyond repo-first
        attribution, including research threads, personal projects, open-floor
        conversations, and explicit general activity."""
        return t.drover_recent_contexts(
            duckdb_path=db,
            container_type=container_type,
            source_harness=source_harness,
            limit=limit,
        )

    @mcp.tool()
    def drover_context_brief(
        context_id: Optional[str] = None,
        label: Optional[str] = None,
    ) -> Optional[dict]:
        """Return one context container by id or label with classification,
        confidence, evidence, open loop, and redaction policy."""
        return t.drover_context_brief(
            duckdb_path=db, context_id=context_id, label=label
        )

    @mcp.tool()
    def drover_open_loops(
        container_type: Optional[str] = None,
        limit: int = 20,
    ) -> dict:
        """Return context containers with known next actions or open loops."""
        return t.drover_open_loops(
            duckdb_path=db, container_type=container_type, limit=limit
        )

    @mcp.tool()
    def drover_resume_context(
        context_id: Optional[str] = None,
        label: Optional[str] = None,
        max_summaries: int = 5,
    ) -> Optional[dict]:
        """Return a context container plus linked session summaries so a local
        agent can resume a non-code or repo-backed thread."""
        return t.drover_resume_context(
            duckdb_path=db,
            context_id=context_id,
            label=label,
            max_summaries=max_summaries,
        )

    @mcp.tool()
    def drover_recall(
        query_embedding: list[float],
        limit: int = 5,
        repo_owner: Optional[str] = None,
        repo_name: Optional[str] = None,
    ) -> dict:
        """Semantic recall: return session summaries ranked by cosine similarity
        to ``query_embedding``. Caller supplies the embedding (encode the query
        with the same model that produced the stored embeddings — typically
        nomic-embed-text via Ollama). Filter by repo if you want recall scoped
        to one project."""
        return t.drover_recall(
            duckdb_path=db,
            query_embedding=query_embedding,
            limit=limit,
            repo_owner=repo_owner,
            repo_name=repo_name,
        )

    @mcp.tool()
    def drover_task_status(task_id: str) -> Optional[dict]:
        """Aggregate stats for a task: session count, agent count, last activity,
        and latest summary. Returns null if the task is unknown."""
        return t.drover_task_status(duckdb_path=db, task_id=task_id)

    @mcp.tool()
    def drover_project_activity(
        project_key: Optional[str] = None,
        since: Optional[str] = None,
        limit: int = 20,
    ) -> dict:
        """Span-level activity grouped by repo and agent. Shows which projects are
        active, their cost, and which agents are working on them.

        ``project_key`` filters to one repo (``owner/name``).
        ``since`` is an ISO-8601 lower bound (default: last 7 days)."""
        return t.drover_project_activity(
            duckdb_path=db, project_key=project_key, since=since, limit=limit
        )

    @mcp.tool()
    def drover_active_handoff(session_id: str, max_age_seconds: float = 60) -> dict:
        """Rolling handoff brief for an OPEN session.

        Returns a compact JSON brief (purpose, last user request, current
        objective, files touched, blockers, suggested next actions) so another
        agent can pick up the work mid-task — without waiting for SessionEnd.

        Results are TTL-cached in ``active_session_briefs``. If the cached
        row is within ``max_age_seconds``, it is returned as-is; otherwise
        the brief is regenerated from the session's last 30 agent_events."""
        return t.drover_active_handoff(
            duckdb_path=db,
            session_id=session_id,
            backend_config=bcfg,
            max_age_seconds=max_age_seconds,
        )

    @mcp.tool()
    def drover_fleet_status() -> dict:
        """Snapshot of all currently-active sessions (event in last 30 min, no
        summary yet) with their repo, agent, and latest user message. Use this
        to answer 'what is every agent doing right now?'"""
        return t.drover_fleet_status(duckdb_path=db)

    @mcp.tool()
    def drover_data_quality(
        incoming_dir: Optional[str] = None,
        hours: int = 24,
        deep: bool = False,
    ) -> dict:
        """Read-only structured lakehouse quality snapshot.

        Returns status, score, category breakdowns, and warnings from the same
        quality_snapshot() implementation used by `drover-server quality`. Use it
        before handoff to check whether Drover data is fresh and complete enough
        to trust. Defaults to standard depth so agent hooks stay responsive; pass
        deep=true for slower operator diagnostics.
        """
        return t.drover_data_quality(
            duckdb_path=db,
            incoming_dir=Path(incoming_dir) if incoming_dir else None,
            hours=hours,
            deep=deep,
        )

    @mcp.tool()
    def drover_pipeline_observatory(
        incoming_dir: Optional[str] = None,
        max_artifacts: int = 10,
        max_projects: int = 10,
    ) -> dict:
        """Read-only Pipeline Observatory drilldown.

        Shows latest saved session-summary and project-brief artifacts, missing
        bundle fields, per-project readiness, and agent adoption state.
        """
        return t.drover_pipeline_observatory(
            duckdb_path=db,
            incoming_dir=Path(incoming_dir) if incoming_dir else None,
            max_artifacts=max_artifacts,
            max_projects=max_projects,
        )

    return mcp
