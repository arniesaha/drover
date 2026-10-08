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
from drover.server.mcp.contract import ReadAdmission
from drover.server.recall_bundle import RecallBundleService
from drover.server.summarizer.backends import SummarizerBackendConfig


def build_mcp_server(
    *,
    duckdb_path: Path,
    name: str = "drover",
    host: str = "127.0.0.1",
    port: int = 7077,
    backend_config: Optional[SummarizerBackendConfig] = None,
    spans_enabled: bool = False,
    embedding_model: Optional[str] = None,
) -> FastMCP:
    """Construct a FastMCP server with all Drover tools registered.

    ``backend_config`` is optional — it's only needed by tools that call
    out to an LLM on demand (currently ``drover_active_handoff``). If
    unset, those tools will raise at call-time.

    ``spans_enabled`` mirrors ``[telemetry] spans_enabled``: off, no tool
    reads span Parquet or span embeddings (#473).
    ``embedding_model`` names the embedding space ``drover_recall`` searches
    (the model the embedding worker writes session vectors with). Without it,
    recall falls back to keyword matching over summaries.
    """
    mcp = FastMCP(name, host=host, port=port)
    db = Path(duckdb_path)
    bcfg = backend_config
    admission = ReadAdmission(path=db)

    def read_tool():
        def register(fn):
            return mcp.tool()(admission.wrap(fn))

        return register

    recall_service = RecallBundleService(
        duckdb_path=db,
    )

    @mcp.tool()
    def drover_profile_propose(
        layer: str,
        kind: str,
        tier: str,
        body: str,
        session_id: Optional[str] = None,
        item_id: Optional[str] = None,
        expires_at: Optional[str] = None,
    ) -> dict:
        """Propose a profile change as a general reader; user approval is required."""
        from drover.server.profile import propose_profile

        return propose_profile(
            db,
            dict(layer=layer, kind=kind, tier=tier, body=body, expires_at=expires_at),
            session_id=session_id,
            item_id=item_id,
        )

    @read_tool()
    def drover_profile(scope: str = "first_turn") -> dict:
        """Load a bounded portable profile. This unauthenticated transport is general."""
        return t.drover_profile(duckdb_path=db, scope=scope)

    @read_tool()
    def drover_memory_acceptance(harness_ids: list[str]) -> dict:
        """Read-only memory evidence report for up to 25 harness IDs."""
        return t.drover_memory_acceptance(duckdb_path=db, harness_ids=harness_ids)

    @read_tool()
    def drover_handoff(
        repo_owner: Optional[str] = None,
        repo_name: Optional[str] = None,
        branch: Optional[str] = None,
        task_id: Optional[str] = None,
        max_summaries: int = 3,
        session_id: Optional[str] = None,
    ) -> dict:
        """Return recent session summaries and currently-active sessions for a
        task, identified by ``task_id``, repo, or a harness/native ``session_id``.
        """
        return t.drover_handoff(
            duckdb_path=db,
            session_id=session_id,
            repo_owner=repo_owner,
            repo_name=repo_name,
            branch=branch,
            task_id=task_id,
            max_summaries=max_summaries,
        )

    @read_tool()
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

    @read_tool()
    def drover_session_summary(session_id: str) -> dict:
        """Return the session_summaries row for one session, or null if no summary exists."""
        return t.drover_session_summary(duckdb_path=db, session_id=session_id)

    @read_tool()
    def drover_active_sessions(task_id: Optional[str] = None) -> dict:
        """List live harness sessions from authoritative control-plane state."""
        return t.drover_active_sessions(duckdb_path=db, task_id=task_id)

    @read_tool()
    def drover_search(
        query: str,
        task_id: Optional[str] = None,
        repo: Optional[str] = None,
        since: Optional[str] = None,
        limit: int = 50,
        default_since_days: int = 30,
        session_id: Optional[str] = None,
    ) -> dict:
        """Case-insensitive content search across agent_events.

        ``session_id`` scopes either a harness or native session identity.
        ``repo`` matches the literal ``<owner>/<name>`` (e.g. ``arniesaha/drover``).
        ``since`` is an ISO-8601 timestamp lower bound. Unscoped searches default
        to the last ``default_since_days`` days to keep live MCP recall bounded.
        """
        return t.drover_search(
            duckdb_path=db,
            session_id=session_id,
            query=query,
            task_id=task_id,
            repo=repo,
            since=since,
            limit=limit,
            default_since_days=default_since_days,
        )

    @read_tool()
    def drover_recall_bundle(
        query: str,
        repo: Optional[str] = None,
        since: Optional[str] = None,
        limit: Optional[int] = None,
        max_context_chars: Optional[int] = None,
    ) -> dict:
        """Return bounded hub recall with scoped Drover context.

        ``since`` is an exact ``YYYY-MM-DD`` lower-bound date.
        Results identify their source as hub.
        """
        return recall_service.recall_bundle(
            query=query,
            repo=repo,
            since=since,
            limit=limit,
            max_context_chars=max_context_chars,
        )

    @read_tool()
    def drover_files_touched(
        limit: int = 100,
        task_id: Optional[str] = None,
        since: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> dict:
        """Return file paths from normalized tool inputs for a task or harness/native ``session_id``."""
        return t.drover_files_touched(
            duckdb_path=db,
            task_id=task_id,
            since=since,
            session_id=session_id,
            limit=limit,
        )

    @mcp.tool()
    def drover_session_close(session_id: str) -> dict:
        """Enqueue a source-versioned summary generation for the session.

        ``status`` is the job ledger's outcome: queued, requeued,
        already_queued, already_done, already_failed, suppressed, or
        unavailable (no PostgreSQL memory store)."""
        return t.drover_session_close(duckdb_path=db, session_id=session_id)

    @read_tool()
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

    @read_tool()
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

    @read_tool()
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

    @read_tool()
    def drover_context_brief(
        context_id: Optional[str] = None,
        label: Optional[str] = None,
    ) -> Optional[dict]:
        """Return one context container by id or label with classification,
        confidence, evidence, open loop, and redaction policy."""
        return t.drover_context_brief(
            duckdb_path=db, context_id=context_id, label=label
        )

    @read_tool()
    def drover_open_loops(
        container_type: Optional[str] = None,
        limit: int = 20,
        project_key: Optional[str] = None,
    ) -> dict:
        """Return context containers with known next actions or open loops.

        ``project_key`` optionally scopes results to one exact ``owner/name``
        repository pair.
        """
        return t.drover_open_loops(
            duckdb_path=db,
            container_type=container_type,
            limit=limit,
            project_key=project_key,
        )

    @read_tool()
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

    @read_tool()
    def drover_recall(
        query_embedding_model: Optional[str] = None,
        query_embedding: Optional[list[float]] = None,
        limit: int = 5,
        repo_owner: Optional[str] = None,
        repo_name: Optional[str] = None,
        session_id: Optional[str] = None,
        query: Optional[str] = None,
    ) -> dict:
        """Semantic recall: return session summaries ranked by cosine similarity
        to ``query_embedding``. Caller supplies the embedding (encode the query
        with the same model that produced the stored embeddings — typically
        nomic-embed-text via Ollama, 768 dimensions). Pass ``query`` as a
        keyword fallback for when semantic search is unavailable; ``mode`` and
        ``reason`` in the response say which ran. Filter by repo if you want
        recall scoped to one project."""
        return t.drover_recall(
            duckdb_path=db,
            session_id=session_id,
            query_embedding=query_embedding,
            limit=limit,
            repo_owner=repo_owner,
            repo_name=repo_name,
            query=query,
            embedding_model=embedding_model,
            query_embedding_model=query_embedding_model,
        )

    @read_tool()
    def drover_task_status(
        task_id: Optional[str] = None, session_id: Optional[str] = None
    ) -> dict:
        """Aggregate stats for a task: session count, agent count, last activity,
        and latest summary. Accepts a harness/native ``session_id``; missing data has an explicit status.
        """
        return t.drover_task_status(
            duckdb_path=db, task_id=task_id, session_id=session_id
        )

    @read_tool()
    def drover_project_activity(
        project_key: Optional[str] = None,
        since: Optional[str] = None,
        days: Optional[int] = None,
        limit: int = 20,
    ) -> dict:
        """What happened on a project recently and what is still open.

        Per-project counts (sessions, active hours, tokens), a timeline of
        sessions grouped by day (title, harness, host, state, duration,
        branch), and open items (sessions waiting or failed, latest next steps
        and open questions). ``project_key`` filters to one repo
        (``owner/name``). ``since`` is an ISO-8601 lower bound, or ``days``
        (default 7, max 30). ``limit`` caps timeline sessions (max 200)."""
        return t.drover_project_activity(
            duckdb_path=db,
            project_key=project_key,
            since=since,
            days=days,
            limit=limit,
        )

    @read_tool()
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

    @read_tool()
    def drover_fleet_status() -> dict:
        """Live fleet from authoritative control-plane harness sessions and hosts."""
        return t.drover_fleet_status(duckdb_path=db)

    @read_tool()
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
            spans_enabled=spans_enabled,
        )

    @read_tool()
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
            spans_enabled=spans_enabled,
        )

    @read_tool()
    def drover_provider_quota(
        provider: Optional[str] = None,
        fresh: bool = False,
    ) -> dict:
        """Read-only provider quota for every account across hosts with routing hint.

        Optional args:
        - provider: filter to a specific provider ('google', 'openai', 'anthropic')
        - fresh: force a re-probe of online hosts bounded by a timeout
        """
        return t.drover_provider_quota(
            duckdb_path=db,
            provider=provider,
            fresh=fresh,
        )

    return mcp
