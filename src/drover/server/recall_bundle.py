"""Compose bounded Pond neighborhoods with explicitly scoped Drover context."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any

from drover.config import ArchiveConfig
from drover.server.archive import (
    ArchiveError,
    ArchiveMessage,
    ArchiveMessageNeighborhood,
    ArchiveMessageRequest,
    ArchiveSearchHit,
    ArchiveSearchRequest,
    SessionArchive,
)
from drover.server.mcp.tools import (
    drover_open_loops,
    drover_project_brief,
    drover_recent_sessions,
    drover_search,
    drover_session_summary,
)

_MAX_CALLER_LIMIT = 20
_MIN_CONTEXT_CHARS = 1_000
_MAX_CONTEXT_CHARS = 100_000
_ARCHIVE_ENRICHMENT_SLOT = threading.BoundedSemaphore(1)


class RecallBundleService:
    """Build one JSON-serializable recall result without cross-store scoring."""

    def __init__(
        self,
        *,
        duckdb_path: Path,
        archive_config: ArchiveConfig,
        archive: SessionArchive | None,
        archive_slot: Any | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._duckdb_path = Path(duckdb_path)
        self._archive_config = archive_config
        self._archive = archive
        self._archive_slot = (
            _ARCHIVE_ENRICHMENT_SLOT if archive_slot is None else archive_slot
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def recall_bundle(
        self,
        query: str,
        repo: str | None = None,
        since: str | None = None,
        limit: int | None = None,
        max_context_chars: int | None = None,
    ) -> dict:
        """Return ranked archive evidence plus independently labeled Drover data."""
        normalized_query = _normalize_query(query)
        normalized_since = _validate_since(since)
        if repo is not None and not isinstance(repo, str):
            raise ValueError("repo must be a string or null")

        requested_limit = _validate_requested_limit(
            limit, default=self._archive_config.search_limit
        )
        effective_limit = min(requested_limit, self._archive_config.search_limit)
        requested_chars = _validate_requested_context_chars(
            max_context_chars, default=self._archive_config.max_context_chars
        )
        effective_chars = min(requested_chars, self._archive_config.max_context_chars)
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
        if not self._archive_config.enabled:
            return self._build_projected_bundle(
                **build_arguments,
                archive_status="disabled",
                search_latency_ms=0,
                matched_total=0,
                searchable_in_scope=0,
                has_more=False,
                selected=(),
                archive_evidence=[],
                warnings=[],
            )
        if self._archive is None:
            return self._build_projected_bundle(
                **build_arguments,
                archive_status="unavailable",
                search_latency_ms=0,
                matched_total=0,
                searchable_in_scope=0,
                has_more=False,
                selected=(),
                archive_evidence=[],
                warnings=[{"category": "unavailable"}],
            )
        if not self._archive_slot.acquire(timeout=self._archive_config.timeout_seconds):
            return self._build_projected_bundle(
                **build_arguments,
                archive_status="busy",
                search_latency_ms=0,
                matched_total=0,
                searchable_in_scope=0,
                has_more=False,
                selected=(),
                archive_evidence=[],
                warnings=[],
            )

        # This private frame owns every decoded search and hydration value.
        # Assign only its bounded dictionary result in the caller so its frame
        # and archive intermediates are gone before the slot is released.
        try:
            bundle = self._build_archive_bundle(**build_arguments)
            return bundle
        finally:
            self._archive_slot.release()

    def _build_archive_bundle(
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
        archive = self._archive
        assert archive is not None
        search_started = perf_counter()
        result = None
        search_warning = None
        try:
            result = archive.search(
                ArchiveSearchRequest(
                    query=query,
                    project=repo,
                    since=since,
                    limit=effective_limit,
                )
            )
        except ArchiveError as error:
            search_warning = {"category": error.category}
        except Exception:
            search_warning = {"category": "protocol_error"}
        search_latency_ms = max(0, int((perf_counter() - search_started) * 1_000))
        if result is None:
            warning = search_warning or {"category": "protocol_error"}
            return self._build_projected_bundle(
                query=query,
                repo=repo,
                since=since,
                requested_limit=requested_limit,
                effective_limit=effective_limit,
                requested_chars=requested_chars,
                effective_chars=effective_chars,
                retrieval_timestamp=retrieval_timestamp,
                archive_status="unavailable",
                search_latency_ms=search_latency_ms,
                matched_total=0,
                searchable_in_scope=0,
                has_more=False,
                selected=(),
                archive_evidence=[],
                warnings=[warning],
            )

        selected = _distinct_hits(result.hits, limit=effective_limit)

        archive_evidence: list[dict] = []
        warnings: list[dict] = []
        for hit in selected:
            warning_category = None
            try:
                neighborhood = archive.get_message(
                    ArchiveMessageRequest(
                        message_id=hit.message_id,
                        context_before=self._archive_config.context_before,
                        context_after=self._archive_config.context_after,
                    )
                )
            except ArchiveError as error:
                warning_category = error.category
            except Exception:
                warning_category = "protocol_error"
            else:
                archive_evidence.append(
                    _project_archive_neighborhood(
                        hit, neighborhood, retrieval_timestamp=retrieval_timestamp
                    )
                )
            if warning_category is not None:
                warnings.append(
                    {"message_id": hit.message_id, "category": warning_category}
                )

        return self._build_projected_bundle(
            query=query,
            repo=repo,
            since=since,
            requested_limit=requested_limit,
            effective_limit=effective_limit,
            requested_chars=requested_chars,
            effective_chars=effective_chars,
            retrieval_timestamp=retrieval_timestamp,
            archive_status="partial" if warnings else "available",
            search_latency_ms=search_latency_ms,
            matched_total=result.matched_total,
            searchable_in_scope=result.searchable_in_scope,
            has_more=result.has_more,
            selected=selected,
            archive_evidence=archive_evidence,
            warnings=warnings,
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
        archive_status: str,
        search_latency_ms: int,
        matched_total: int,
        searchable_in_scope: int,
        has_more: bool,
        selected: tuple[ArchiveSearchHit, ...],
        archive_evidence: list[dict],
        warnings: list[dict],
    ) -> dict:

        exact_session_ids = {hit.session_id for hit in selected}
        drover_context = self._build_drover_context(
            query=query,
            repo=repo,
            since=since,
            limit=effective_limit,
            exact_session_ids=exact_session_ids,
            selected=selected,
            retrieval_timestamp=retrieval_timestamp,
        )
        archive_metadata = {
            "status": archive_status,
            "search_latency_ms": search_latency_ms,
            "matched_total": matched_total,
            "searchable_in_scope": searchable_in_scope,
            "has_more": has_more,
            "selected_count": len(selected),
            "hydrated_count": len(archive_evidence),
            "retained_count": len(archive_evidence),
            "result_set_freshness": _newest_selected_timestamp(selected),
            "retrieval_timestamp": retrieval_timestamp,
        }
        if warnings:
            archive_metadata["warnings"] = warnings

        bundle = {
            "query": {"text": query, "repo": repo, "since": since},
            "archive": archive_metadata,
            "archive_evidence": archive_evidence,
            "drover_context": drover_context,
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
        _apply_character_budget(bundle, effective_chars)
        bundle["archive"]["retained_count"] = len(bundle["archive_evidence"])
        return bundle

    def _build_drover_context(
        self,
        *,
        query: str,
        repo: str | None,
        since: str | None,
        limit: int,
        exact_session_ids: set[str],
        selected: tuple[ArchiveSearchHit, ...],
        retrieval_timestamp: str,
    ) -> dict:
        keyword_result = drover_search(
            duckdb_path=self._duckdb_path,
            query=query,
            repo=repo,
            since=since,
            limit=limit,
        )
        keyword_matches = [
            _project_keyword_match(
                row,
                retrieval_timestamp=retrieval_timestamp,
                exact_session_ids=exact_session_ids,
            )
            for row in keyword_result["results"]
            if _content(row.get("content"))
        ]

        exact_summaries: list[dict] = []
        seen_sessions: set[str] = set()
        for hit in selected:
            if hit.session_id in seen_sessions:
                continue
            seen_sessions.add(hit.session_id)
            row = drover_session_summary(
                duckdb_path=self._duckdb_path, session_id=hit.session_id
            )
            if row is not None:
                item = _project_session_summary(
                    row,
                    retrieval_timestamp=retrieval_timestamp,
                    join_basis="exact_session_id",
                    archive_rank=hit.rank,
                )
                if item is not None:
                    exact_summaries.append(item)

        project_brief_item: dict | None = None
        recent_summary_items: list[dict] = []
        open_loop_items: list[dict] = []
        if _is_exact_repository(repo):
            brief = drover_project_brief(
                duckdb_path=self._duckdb_path, project_key=repo
            )
            if brief is not None:
                project_brief_item = _project_brief(
                    brief, retrieval_timestamp=retrieval_timestamp
                )

            recent = drover_recent_sessions(
                duckdb_path=self._duckdb_path,
                project_key=repo,
                limit=limit,
            )
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
            "exact_session_summaries": exact_summaries,
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
    if not isinstance(since, str) or not since:
        raise ValueError("since must be an ISO-8601 string")
    try:
        datetime.fromisoformat(_z_to_offset(since))
    except ValueError as exc:
        raise ValueError("since must be an ISO-8601 string") from exc
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


def _distinct_hits(
    hits: Iterable[ArchiveSearchHit], *, limit: int
) -> tuple[ArchiveSearchHit, ...]:
    selected: list[ArchiveSearchHit] = []
    seen_message_ids: set[str] = set()
    for hit in hits:
        if hit.message_id in seen_message_ids:
            continue
        seen_message_ids.add(hit.message_id)
        selected.append(hit)
        if len(selected) == limit:
            break
    return tuple(selected)


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


def _project_archive_neighborhood(
    hit: ArchiveSearchHit,
    neighborhood: ArchiveMessageNeighborhood,
    *,
    retrieval_timestamp: str,
) -> dict:
    target = _source_item(
        source_type="pond_message",
        source_identifiers={
            "message_id": hit.message_id,
            "session_id": hit.session_id,
            "project": hit.project,
        },
        source_agent=hit.source_agent,
        source_timestamp=hit.timestamp,
        retrieval_timestamp=retrieval_timestamp,
        join_basis="archive_rank",
        text=hit.text,
    )
    target.update(
        {
            "rank": hit.rank,
            "role": hit.role,
            "context_truncated": False,
            "session": {
                "session_id": neighborhood.session.session_id,
                "project": neighborhood.session.project,
                "source_agent": neighborhood.session.source_agent,
                "created_at": neighborhood.session.created_at,
                "parent_session_id": neighborhood.session.parent_session_id,
                "parent_message_id": neighborhood.session.parent_message_id,
            },
            "target_part_count": neighborhood.target_part_count,
            "target_parts_remaining": neighborhood.target_parts_remaining,
            "context_before": neighborhood.context_before,
            "context_after": neighborhood.context_after,
            "siblings": [
                _project_archive_sibling(
                    sibling, retrieval_timestamp=retrieval_timestamp
                )
                for sibling in neighborhood.siblings
                if sibling.text
            ],
        }
    )
    return target


def _project_archive_sibling(
    message: ArchiveMessage, *, retrieval_timestamp: str
) -> dict:
    item = _source_item(
        source_type="pond_message",
        source_identifiers={
            "message_id": message.message_id,
            "session_id": message.session_id,
            "project": message.project,
        },
        source_agent=message.source_agent,
        source_timestamp=message.timestamp,
        retrieval_timestamp=retrieval_timestamp,
        join_basis="archive_rank",
        text=message.text or "",
    )
    item["role"] = message.role
    return item


def _project_keyword_match(
    row: dict,
    *,
    retrieval_timestamp: str,
    exact_session_ids: set[str],
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
        join_basis=(
            "exact_session_id"
            if session_id in exact_session_ids
            else "drover_keyword_match"
        ),
        text=_content(row.get("content")),
    )
    item["event_type"] = row.get("event_type")
    return item


def _project_session_summary(
    row: dict,
    *,
    retrieval_timestamp: str,
    join_basis: str,
    archive_rank: int | None = None,
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
        source_timestamp=row.get("ended_at") or row.get("generated_at"),
        retrieval_timestamp=retrieval_timestamp,
        join_basis=join_basis,
        text=text,
    )
    if archive_rank is not None:
        item["archive_rank"] = archive_rank
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
        source_timestamp=row.get("generated_at") or row.get("last_activity_at"),
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
    return _source_item(
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


def _newest_selected_timestamp(hits: tuple[ArchiveSearchHit, ...]) -> str | None:
    parseable: list[tuple[datetime, str]] = []
    for hit in hits:
        try:
            parsed = datetime.fromisoformat(_z_to_offset(hit.timestamp))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        parseable.append((parsed.astimezone(timezone.utc), hit.timestamp))
    if not parseable:
        return None
    return max(parseable, key=lambda item: item[0])[1]


def _z_to_offset(value: str) -> str:
    return f"{value[:-1]}+00:00" if value.endswith("Z") else value


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
    archive_items = [
        item
        for neighborhood in bundle["archive_evidence"]
        for item in (neighborhood, *neighborhood["siblings"])
        if item.get("text")
    ]
    context = bundle["drover_context"]
    brief = [context["project_brief"]] if context["project_brief"] else []
    return [
        *archive_items,
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
    if bundle["archive_evidence"]:
        return bundle["archive_evidence"][0]
    return None


def _apply_character_budget(bundle: dict, maximum: int) -> None:
    dropped = bundle["limits"]["dropped"]
    keeper = _highest_priority_item(bundle)

    for neighborhood in list(reversed(bundle["archive_evidence"])):
        if _used_chars(bundle) <= maximum:
            break
        if keeper is neighborhood or keeper in neighborhood["siblings"]:
            continue
        bundle["archive_evidence"].remove(neighborhood)
        dropped["archive_neighborhoods"] += 1

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
    bundle["limits"]["truncated"] = (
        bool(sum(dropped.values()))
        or any(
            neighborhood["context_truncated"]
            for neighborhood in bundle["archive_evidence"]
        )
        or any(item["truncated"] for item in _text_items(bundle))
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
    kept_archive: list[dict] = []
    for neighborhood in bundle["archive_evidence"]:
        if neighborhood is keeper:
            dropped["archive_neighborhoods"] += len(bundle["archive_evidence"]) - 1
            removed_siblings = len(neighborhood["siblings"])
            dropped["archive_siblings"] += removed_siblings
            neighborhood["context_truncated"] = removed_siblings > 0
            neighborhood["siblings"] = []
            kept_archive = [neighborhood]
            break
    else:
        dropped["archive_neighborhoods"] += len(bundle["archive_evidence"])
    bundle["archive_evidence"] = kept_archive

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
