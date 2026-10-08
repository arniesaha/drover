"""Compose bounded hub recall with explicitly scoped Drover context."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from drover.server.mcp.freshness import with_freshness
from drover.server.mcp.tools import (
    drover_open_loops,
    drover_project_brief,
    drover_recent_sessions,
    drover_search,
)

_MAX_CALLER_LIMIT = 20
_MIN_CONTEXT_CHARS = 1_000
_MAX_CONTEXT_CHARS = 100_000


class RecallBundleService:
    """Build one JSON-serializable recall result from hub context."""

    def __init__(
        self,
        *,
        duckdb_path: Path,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._duckdb_path = Path(duckdb_path)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def recall_bundle(
        self,
        query: str,
        repo: str | None = None,
        since: str | None = None,
        limit: int | None = None,
        max_context_chars: int | None = None,
    ) -> dict:
        """Return bounded, source-labeled hub context."""
        normalized_query = _normalize_query(query)
        normalized_since = _validate_since(since)
        if repo is not None and not isinstance(repo, str):
            raise ValueError("repo must be a string or null")

        requested_limit = _validate_requested_limit(limit, default=5)
        effective_limit = min(requested_limit, 5)
        requested_chars = _validate_requested_context_chars(
            max_context_chars, default=24_000
        )
        effective_chars = min(requested_chars, 24_000)
        retrieval_timestamp = _retrieval_timestamp(self._clock)

        build_arguments = {
            "query": normalized_query,
            "repo": repo,
            "since": normalized_since,
            "requested_limit": requested_limit,
            "effective_limit": effective_limit,
            "requested_chars": requested_chars,
            "effective_chars": effective_chars,
            "retrieval_timestamp": retrieval_timestamp,
        }
        from drover.server.lake.runtime import LakeError
        from drover.server.lake.serving import selected_config

        config = selected_config(self._duckdb_path)
        try:
            from drover.server.lake.coverage import read_fence

            with read_fence(self._duckdb_path):
                config = selected_config(self._duckdb_path)
                if config.backend == "ducklake" and _is_exact_repository(repo):
                    from drover.server.lake.read_models import read_model

                    # Missing authoritative context publication is explicit and
                    # bound to the verified selection, never an empty legacy mix.
                    coverage = read_model(
                        self._duckdb_path, "contexts", limit=effective_limit
                    )
                    if coverage.get("status") == "unavailable":
                        return with_freshness(coverage, path=self._duckdb_path)
                bundle = self._build_projected_bundle(**build_arguments)
                if config.backend == "ducklake":
                    from drover.server.lake.coverage import bounded
                    from drover.server.lake.read_models import read_model

                    if not _is_exact_repository(repo):
                        coverage = read_model(self._duckdb_path, "coverage")
                    bundle["metadata"] = coverage["metadata"]
                    bounded(bundle)
                return with_freshness(bundle, path=self._duckdb_path)
        except LakeError as exc:
            return with_freshness(
                {
                    "status": "unavailable",
                    "analytics_backend": config.backend,
                    "analytics_epoch": config.epoch,
                    "reason": exc.code,
                },
                path=self._duckdb_path,
            )

    def _build_projected_bundle(
        self,
        *,
        query: str,
        repo: str | None,
        since: str | None,
        requested_limit: int,
        effective_limit: int,
        requested_chars: int,
        effective_chars: int,
        retrieval_timestamp: str,
    ) -> dict:

        archive_metadata = {
            "status": "removed",
            "search_latency_ms": 0,
            "matched_total": 0,
            "searchable_in_scope": 0,
            "has_more": False,
            "selected_count": 0,
            "hydrated_count": 0,
            "retained_count": 0,
            "result_set_freshness": None,
            "retrieval_timestamp": retrieval_timestamp,
        }
        bundle = {
            "query": {"text": query, "repo": repo, "since": since},
            "archive": archive_metadata,
            "archive_evidence": [],
            "drover_context": self._build_drover_context(
                query=query,
                repo=repo,
                since=since,
                limit=effective_limit,
                retrieval_timestamp=retrieval_timestamp,
            ),
            "limits": {
                "requested_limit": requested_limit,
                "effective_limit": effective_limit,
                "requested_max_context_chars": requested_chars,
                "effective_max_context_chars": effective_chars,
                "used_chars": 0,
                "truncated": False,
                "dropped": _empty_drop_counts(),
            },
        }
        bundle["sources"] = ["hub"]
        _apply_character_budget(bundle, effective_chars)
        return bundle

    def _build_drover_context(
        self,
        *,
        query: str,
        repo: str | None,
        since: str | None,
        limit: int,
        retrieval_timestamp: str,
    ) -> dict:
        keyword_result = drover_search(
            duckdb_path=self._duckdb_path,
            query=query,
            repo=repo,
            since=since,
            limit=limit,
        )
        if keyword_result.get("status") == "unavailable":
            from drover.server.lake.runtime import LakeError

            raise LakeError(keyword_result.get("reason") or "analytics_unavailable")
        keyword_matches = [
            _project_keyword_match(
                row,
                retrieval_timestamp=retrieval_timestamp,
            )
            for row in keyword_result["results"]
            if _content(row.get("content"))
        ]

        project_brief_item: dict | None = None
        recent_summary_items: list[dict] = []
        open_loop_items: list[dict] = []
        if _is_exact_repository(repo):
            brief = drover_project_brief(
                duckdb_path=self._duckdb_path, project_key=repo
            )
            if brief is not None and brief.get("status") == "unavailable":
                from drover.server.lake.runtime import LakeError

                raise LakeError(brief.get("reason") or "analytics_unavailable")
            if brief is not None:
                project_brief_item = _project_brief(
                    brief, retrieval_timestamp=retrieval_timestamp
                )

            recent = drover_recent_sessions(
                duckdb_path=self._duckdb_path,
                project_key=repo,
                limit=limit,
            )
            if recent.get("status") == "unavailable":
                from drover.server.lake.runtime import LakeError

                raise LakeError(recent.get("reason") or "analytics_unavailable")
            recent_summary_items = [
                projected
                for row in recent["sessions"]
                if (
                    projected := _project_session_summary(
                        row,
                        retrieval_timestamp=retrieval_timestamp,
                        join_basis="caller_repo_scope",
                    )
                )
                is not None
            ]

            loops = drover_open_loops(
                duckdb_path=self._duckdb_path,
                project_key=repo,
                limit=limit,
            )
            if loops.get("status") == "unavailable":
                from drover.server.lake.runtime import LakeError

                raise LakeError(loops.get("reason") or "analytics_unavailable")
            open_loop_items = [
                projected
                for row in loops["open_loops"]
                if (
                    projected := _project_open_loop(
                        row, retrieval_timestamp=retrieval_timestamp
                    )
                )
                is not None
            ]

        return {
            "keyword_matches": keyword_matches,
            "exact_session_summaries": [],
            "project_brief": project_brief_item,
            "repository_recent_summaries": recent_summary_items,
            "repository_open_loops": open_loop_items,
        }


def _normalize_query(query: str) -> str:
    if not isinstance(query, str):
        raise ValueError("query must be a non-blank string")
    normalized = " ".join(query.split())
    if not normalized:
        raise ValueError("query must be a non-blank string")
    return normalized


def _validate_since(since: str | None) -> str | None:
    if since is None:
        return None
    if (
        type(since) is not str
        or len(since) != 10
        or since[4] != "-"
        or since[7] != "-"
        or not since.replace("-", "").isdigit()
    ):
        raise ValueError("since must be a valid YYYY-MM-DD date")
    try:
        date.fromisoformat(since)
    except ValueError as exc:
        raise ValueError("since must be a valid YYYY-MM-DD date") from exc
    return since


def _validate_requested_limit(limit: int | None, *, default: int) -> int:
    requested = default if limit is None else limit
    if type(requested) is not int or not 1 <= requested <= _MAX_CALLER_LIMIT:
        raise ValueError("limit must be an integer between 1 and 20")
    return requested


def _validate_requested_context_chars(value: int | None, *, default: int) -> int:
    requested = default if value is None else value
    if (
        type(requested) is not int
        or not _MIN_CONTEXT_CHARS <= requested <= _MAX_CONTEXT_CHARS
    ):
        raise ValueError("max_context_chars must be an integer between 1000 and 100000")
    return requested


def _retrieval_timestamp(clock: Callable[[], datetime]) -> str:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat()


def _source_item(
    *,
    source_type: str,
    source_identifiers: dict[str, Any],
    source_agent: str | None,
    source_timestamp: str | None,
    retrieval_timestamp: str,
    join_basis: str,
    text: str,
) -> dict:
    return {
        "generated_at": (
            source_timestamp
            if source_type in {"session_summary", "project_brief"}
            else None
        ),
        "source_type": source_type,
        "source_identifiers": {
            key: value for key, value in source_identifiers.items() if value is not None
        },
        "source_agent": source_agent,
        "source_timestamp": source_timestamp,
        "retrieval_timestamp": retrieval_timestamp,
        "join_basis": join_basis,
        "truncated": False,
        "text": text,
    }


def _project_keyword_match(
    row: dict,
    *,
    retrieval_timestamp: str,
) -> dict:
    session_id = row.get("session_id")
    item = _source_item(
        source_type="agent_event",
        source_identifiers={
            "event_id": row.get("id"),
            "session_id": session_id,
        },
        source_agent=row.get("agent_id"),
        source_timestamp=row.get("timestamp"),
        retrieval_timestamp=retrieval_timestamp,
        join_basis="drover_keyword_match",
        text=_content(row.get("content")),
    )
    item["event_type"] = row.get("event_type")
    return item


def _project_session_summary(
    row: dict,
    *,
    retrieval_timestamp: str,
    join_basis: str,
) -> dict | None:
    text = _join_content(
        row.get("summary_md"),
        row.get("next_steps_md"),
        row.get("open_questions"),
    )
    if not text:
        return None
    item = _source_item(
        source_type="session_summary",
        source_identifiers={
            "session_id": row.get("session_id"),
            "task_id": row.get("task_id"),
        },
        source_agent=row.get("agent_id"),
        source_timestamp=row.get("generated_at"),
        retrieval_timestamp=retrieval_timestamp,
        join_basis=join_basis,
        text=text,
    )
    return item


def _project_brief(row: dict, *, retrieval_timestamp: str) -> dict | None:
    text = _join_content(
        row.get("brief_md"),
        row.get("recent_themes_md"),
        row.get("key_files"),
        row.get("open_questions"),
        row.get("next_steps_md"),
    )
    if not text:
        return None
    return _source_item(
        source_type="project_brief",
        source_identifiers={
            "project_key": row.get("project_key"),
            "repo_owner": row.get("repo_owner"),
            "repo_name": row.get("repo_name"),
        },
        source_agent=None,
        source_timestamp=row.get("generated_at"),
        retrieval_timestamp=retrieval_timestamp,
        join_basis="caller_repo_scope",
        text=text,
    )


def _project_open_loop(row: dict, *, retrieval_timestamp: str) -> dict | None:
    text = _join_content(
        row.get("summary_md"),
        row.get("next_action"),
        row.get("open_loop"),
        row.get("evidence"),
    )
    if not text:
        return None
    item = _source_item(
        source_type="context_container",
        source_identifiers={
            "context_id": row.get("context_id"),
            "repo_owner": row.get("repo_owner"),
            "repo_name": row.get("repo_name"),
        },
        source_agent=row.get("source_harness"),
        source_timestamp=(
            row.get("last_touched_at") or row.get("updated_at") or row.get("created_at")
        ),
        retrieval_timestamp=retrieval_timestamp,
        join_basis="caller_repo_scope",
        text=text,
    )

    for key in ("store", "host", "data_watermark", "store_authoritative"):
        if key in row:
            item[key] = row[key]
    return item


def _join_content(*values: object) -> str:
    pieces: list[str] = []
    for value in values:
        if isinstance(value, str):
            if value:
                pieces.append(value)
        elif isinstance(value, (list, tuple)):
            pieces.extend(item for item in value if isinstance(item, str) and item)
    return "\n".join(pieces)


def _content(value: object) -> str:
    return value if isinstance(value, str) else ""


def _is_exact_repository(repo: str | None) -> bool:
    if repo is None or repo.count("/") != 1:
        return False
    owner, name = repo.split("/", 1)
    return bool(owner and name and owner == owner.strip() and name == name.strip())


def _empty_drop_counts() -> dict[str, int]:
    return {
        "archive_neighborhoods": 0,
        "archive_siblings": 0,
        "repository_open_loops": 0,
        "repository_recent_summaries": 0,
        "project_brief": 0,
        "drover_keyword_matches": 0,
        "exact_session_summaries": 0,
    }


def _text_items(bundle: dict) -> list[dict]:
    context = bundle["drover_context"]
    brief = [context["project_brief"]] if context["project_brief"] else []
    return [
        *context["keyword_matches"],
        *context["exact_session_summaries"],
        *brief,
        *context["repository_recent_summaries"],
        *context["repository_open_loops"],
    ]


def _used_chars(bundle: dict) -> int:
    return sum(len(item["text"]) for item in _text_items(bundle))


def _highest_priority_item(bundle: dict) -> dict | None:
    context = bundle["drover_context"]
    for collection_name in ("exact_session_summaries", "keyword_matches"):
        if context[collection_name]:
            return context[collection_name][0]
    if context["project_brief"]:
        return context["project_brief"]
    for collection_name in (
        "repository_recent_summaries",
        "repository_open_loops",
    ):
        if context[collection_name]:
            return context[collection_name][0]
    return None


def _apply_character_budget(bundle: dict, maximum: int) -> None:
    dropped = bundle["limits"]["dropped"]
    keeper = _highest_priority_item(bundle)

    context = bundle["drover_context"]
    _drop_from_end(
        bundle,
        context["repository_open_loops"],
        maximum=maximum,
        keeper=keeper,
        counter=dropped,
        counter_key="repository_open_loops",
    )
    _drop_from_end(
        bundle,
        context["repository_recent_summaries"],
        maximum=maximum,
        keeper=keeper,
        counter=dropped,
        counter_key="repository_recent_summaries",
    )
    if (
        _used_chars(bundle) > maximum
        and context["project_brief"] is not None
        and context["project_brief"] is not keeper
    ):
        context["project_brief"] = None
        dropped["project_brief"] += 1
    _drop_from_end(
        bundle,
        context["keyword_matches"],
        maximum=maximum,
        keeper=keeper,
        counter=dropped,
        counter_key="drover_keyword_matches",
    )
    _drop_from_end(
        bundle,
        context["exact_session_summaries"],
        maximum=maximum,
        keeper=keeper,
        counter=dropped,
        counter_key="exact_session_summaries",
    )

    if _used_chars(bundle) > maximum and keeper is not None:
        _retain_only(bundle, keeper, dropped=dropped)
        if _used_chars(bundle) > maximum:
            keeper["text"] = keeper["text"][:maximum]
            keeper["truncated"] = True

    used = _used_chars(bundle)
    bundle["limits"]["used_chars"] = used
    bundle["limits"]["truncated"] = bool(sum(dropped.values())) or any(
        item["truncated"] for item in _text_items(bundle)
    )


def _drop_from_end(
    bundle: dict,
    items: list[dict],
    *,
    maximum: int,
    keeper: dict | None,
    counter: dict[str, int],
    counter_key: str,
) -> None:
    for item in list(reversed(items)):
        if _used_chars(bundle) <= maximum:
            break
        if item is keeper:
            continue
        items.remove(item)
        counter[counter_key] += 1


def _retain_only(bundle: dict, keeper: dict, *, dropped: dict[str, int]) -> None:
    context = bundle["drover_context"]
    collections = (
        ("keyword_matches", "drover_keyword_matches"),
        ("exact_session_summaries", "exact_session_summaries"),
        ("repository_recent_summaries", "repository_recent_summaries"),
        ("repository_open_loops", "repository_open_loops"),
    )
    for collection_name, counter_name in collections:
        items = context[collection_name]
        retained = [item for item in items if item is keeper]
        dropped[counter_name] += len(items) - len(retained)
        context[collection_name] = retained
    if context["project_brief"] is not keeper and context["project_brief"] is not None:
        context["project_brief"] = None
        dropped["project_brief"] += 1
