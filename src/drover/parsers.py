import glob
import json
import os
import sqlite3
import sys
from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import Any, List, Optional
from uuid import UUID

from drover.agent_aliases import canonicalize
from drover.attribution import (
    _cwd_from_raw,
    derive_repo_attribution,
    enrich_raw_repo_attribution,
)
from drover.models import AgentEvent, Message, ToolCall

_CLAUDE_ENCODED_CWD_PREFIXES = (
    ("-Users-arnabmac-jenny-nexus", "/Users/arnabmac/jenny/nexus"),
    ("-Users-arnabmac-jenny-agent-foundry", "/Users/arnabmac/jenny/agent-foundry"),
    ("-Users-arnabmac-jenny-agent-shared", "/Users/arnabmac/jenny/agent-shared"),
    ("-Users-arnabmac-.hermes-hermes-agent", "/Users/arnabmac/.hermes/hermes-agent"),
    # Observed live Claude Code project directory names encode a leading dot as
    # a double hyphen before the segment. Keep the historical single-dot form as
    # a compatibility alias, but prefer the live double-hyphen spelling.
    (
        "-Users-arnabmac--claude-mem-observer-sessions",
        "/Users/arnabmac/.claude-mem/observer-sessions",
    ),
    (
        "-Users-arnabmac-.claude-mem-observer-sessions",
        "/Users/arnabmac/.claude-mem/observer-sessions",
    ),
    # Max workspaces live under /Users/arnabmac/max. These mappings only recover
    # cwd from Claude's project directory; repo attribution still comes from the
    # existing git/known-root enrichment path.
    (
        "-Users-arnabmac-max-projects-agent-max",
        "/Users/arnabmac/max/projects/agent-max",
    ),
    (
        "-Users-arnabmac-max-projects-agentweave",
        "/Users/arnabmac/max/projects/agentweave",
    ),
    ("-Users-arnabmac-max-projects-NixClaw", "/Users/arnabmac/max/projects/NixClaw"),
    (
        "-Users-arnabmac-projects-pi-mono-agent",
        "/Users/arnabmac/projects/pi-mono-agent",
    ),
    ("-Users-arnabmac", "/Users/arnabmac"),
    ("-home-Arnab-clawd-projects-healthos", "/home/Arnab/clawd/projects/healthos"),
    (
        "-home-Arnab-clawd-projects-ai-ops-studio",
        "/home/Arnab/clawd/projects/ai-ops-studio",
    ),
    ("-home-Arnab-dev-agent-shared", "/home/Arnab/dev/agent-shared"),
    ("-home-Arnab-dev-agentweave", "/home/Arnab/dev/agentweave"),
    ("-home-Arnab-dev-agent-max", "/home/Arnab/dev/agent-max"),
    ("-home-Arnab-dev-openclaw", "/home/Arnab/dev/openclaw"),
    ("-home-Arnab-dev-portfolio", "/home/Arnab/dev/portfolio"),
    ("-home-Arnab-dev-nexus", "/home/Arnab/dev/nexus"),
    ("-home-Arnab-dev-mux", "/home/Arnab/dev/mux"),
    ("-home-Arnab-clawd", "/home/Arnab/clawd"),
    ("-home-Arnab", "/home/Arnab"),
)


def _decode_claude_project_dir_name(name: str) -> Optional[str]:
    """Best-effort decode of Claude Code's project directory cwd encoding.

    Claude stores project JSONL under names such as
    ``-Users-arnabmac-jenny-nexus``. Hyphens are ambiguous because they can be
    either path separators or literal characters in a path segment, so prefer
    known safe prefixes (including hyphenated repo names) and only use the
    generic slash replacement for simple absolute names.
    """
    if not name.startswith("-"):
        return None
    for encoded_prefix, cwd_prefix in sorted(
        _CLAUDE_ENCODED_CWD_PREFIXES, key=lambda item: len(item[0]), reverse=True
    ):
        if name == encoded_prefix or name.startswith(f"{encoded_prefix}-"):
            suffix = name[len(encoded_prefix) :].removeprefix("-")
            if suffix:
                return f"{cwd_prefix}/{suffix.replace('-', '/')}"
            return cwd_prefix
    return name.replace("-", "/")


def _claude_project_cwd_from_file(filepath: str) -> Optional[str]:
    return _decode_claude_project_dir_name(Path(filepath).parent.name)


def parse_claude_audit_log(
    filepath: str, agent_id: str = "nas-claude"
) -> List[AgentEvent]:
    events = []
    inferred_cwd = _claude_project_cwd_from_file(filepath)
    with open(filepath, "r") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = str(data.get("type", "unknown"))
            session_id = (
                data.get("session_id") or data.get("sessionId") or "unknown_session"
            )

            timestamp_str = data.get("_audit_timestamp") or data.get("timestamp")
            if timestamp_str:
                timestamp_str = timestamp_str.replace("Z", "+00:00")
                timestamp = datetime.fromisoformat(timestamp_str)
            else:
                timestamp = datetime.now(UTC)

            uuid = data.get("uuid", f"auto-{timestamp.timestamp()}")

            # Forward the entire source dict into raw_data — Claude Code's
            # JSONL turns carry attribution signals (cwd, gitBranch, version,
            # userType, …) that downstream consumers rely on. Filtering to a
            # subset here is how #48 went wrong: work-macbook-claude lost cwd
            # on 178k rows. ``enrich_raw_repo_attribution`` shallow-copies the
            # dict and only *adds* canonical keys, so every original field
            # round-trips.
            raw_data = dict(data)
            if inferred_cwd and _cwd_from_raw(raw_data) is None:
                raw_data["cwd"] = inferred_cwd

            event = AgentEvent(
                id=uuid,
                session_id=session_id,
                timestamp=timestamp,
                agent_id=agent_id,
                event_type=event_type,
                raw_data=enrich_raw_repo_attribution(raw_data),
            )

            if "message" in data:
                msg_data = data["message"]
                if isinstance(msg_data, dict):
                    role = msg_data.get("role", "unknown")
                    content = msg_data.get("content", "")
                    event.message = Message(role=role, content=content)

                    if role == "assistant" and isinstance(content, list):
                        tool_calls = []
                        for block in content:
                            if block.get("type") == "tool_use":
                                tool_calls.append(
                                    ToolCall(
                                        tool_name=block.get("name", "unknown"),
                                        input=block.get("input", {}),
                                    )
                                )
                        if tool_calls:
                            event.tool_calls = tool_calls

                    if role == "assistant" and "usage" in msg_data:
                        event.token_usage = msg_data["usage"]
            events.append(event)
    return events


def parse_hermes_sessions(filepath: str) -> List[AgentEvent]:
    """Parse a single Hermes JSON session file."""
    events = []
    with open(filepath, "r") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            return []

        session_id = data.get("session_id", "unknown_hermes_session")
        for idx, msg in enumerate(data.get("messages", [])):
            role = msg.get("role", "unknown")
            content = msg.get("content", "")

            timestamp_str = msg.get("timestamp") or data.get("session_start")
            if timestamp_str:
                try:
                    timestamp = datetime.fromisoformat(timestamp_str)
                except ValueError:
                    timestamp = datetime.now(UTC)
            else:
                timestamp = datetime.now(UTC)

            uuid = f"{session_id}-msg-{idx}"
            event = AgentEvent(
                id=uuid,
                session_id=session_id,
                timestamp=timestamp,
                agent_id="macmini-hermes",
                event_type=role,
                message=Message(role=role, content=content),
                raw_data=msg,
            )
            events.append(event)
    return events


def parse_task_journal(db_path: str) -> List[AgentEvent]:
    """Parse pi-mono task-journal.db"""
    events = []
    if not os.path.exists(db_path):
        return events

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM tasks ORDER BY created_at ASC")
        rows = cursor.fetchall()
        for row in rows:
            task_id = row["id"]
            created_at = row["created_at"]
            if created_at > 9999999999:
                timestamp = datetime.fromtimestamp(created_at / 1000.0)
            else:
                timestamp = datetime.fromtimestamp(created_at)

            task_type = row["type"]
            source = row["source"]
            payload = row["payload"]
            try:
                payload_data = json.loads(payload)
                content = (
                    payload_data.get("message", payload)
                    if isinstance(payload_data, dict)
                    else payload
                )
            except Exception:
                content = payload
                payload_data = {"raw": payload}

            event = AgentEvent(
                id=f"pi-mono-{task_id}",
                session_id=f"pi-mono-{source}",
                timestamp=timestamp,
                agent_id="max-pimono",
                event_type=f"task_{task_type}",
                message=Message(role="user", content=content),
                raw_data={
                    "id": task_id,
                    "type": task_type,
                    "source": source,
                    "status": row["status"],
                    "payload": payload_data,
                    "result": row["result"],
                },
            )
            events.append(event)
    except sqlite3.Error:
        pass
    finally:
        conn.close()
    return events


def _openclaw_nested(data: dict, *path: str) -> Any:
    cur: Any = data
    for part in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _first_openclaw_value(data: dict, *keys: str) -> Any:
    for key in keys:
        if "." in key:
            value = _openclaw_nested(data, *key.split("."))
        else:
            value = data.get(key)
        if value not in (None, ""):
            return value
    return None


def _is_uuid(value: Any) -> bool:
    if value in (None, ""):
        return False
    try:
        UUID(str(value))
    except (TypeError, ValueError):
        return False
    return True


def _openclaw_session_identity(
    data: dict,
    current_session_id: str,
    current_session_key: Optional[str],
    current_uuid_missing: bool,
) -> tuple[str, Optional[str], bool]:
    canonical = _first_openclaw_value(
        data,
        "session_uuid",
        "sessionUuid",
        "session.id",
        "sessionId",
    )
    if (
        canonical in (None, "")
        and data.get("type") == "session"
        and _is_uuid(data.get("id"))
    ):
        canonical = data.get("id")
    session_key = _first_openclaw_value(
        data,
        "session_key",
        "sessionKey",
        "routeKey",
        "channelKey",
        "session.key",
    )

    if canonical:
        canonical = str(canonical)
        return (
            canonical,
            (
                str(session_key)
                if session_key
                else current_session_key if canonical == current_session_id else None
            ),
            False,
        )

    # Legacy OpenClaw files only exposed a session header id. Preserve the old
    # stable session_id behavior, but mark that no canonical UUID was available
    # so route/session keys can be reconciled later without pretending they are
    # durable UUIDs.
    fallback = current_session_id
    uuid_missing = current_uuid_missing
    if data.get("type") == "session" and data.get("id"):
        fallback = str(data["id"])
        uuid_missing = True
    return (
        fallback,
        str(session_key) if session_key else current_session_key,
        uuid_missing,
    )


def _normalize_openclaw_event_type(source_type: Any, data: dict) -> str:
    source = str(source_type or "unknown")
    lowered = source.lower().replace(":", ".")
    role = _openclaw_nested(data, "message", "role")

    # Preserve the legacy OpenClaw row vocabulary for old exports.
    if source == "message":
        return "message"
    if lowered in {"message.queued", "user.message", "user_turn"} or role == "user":
        return "user_turn"
    if (
        lowered in {"message.processed", "assistant.message", "assistant_turn"}
        or role == "assistant"
    ):
        return "assistant_turn"
    if "tool.call" in lowered or lowered in {"tool_call", "tool.use"}:
        return "tool_call"
    if "tool.result" in lowered or lowered in {"tool_result", "tool.observation"}:
        return "tool_result"
    if lowered.startswith("command"):
        return "command"
    if "error" in lowered or lowered.endswith(".failed"):
        return "error"
    if lowered.startswith("session") or any(
        token in lowered for token in ("bootstrap", "lifecycle", "plugin", "hook")
    ):
        if "end" in lowered or "closed" in lowered or "archive" in lowered:
            return "session_end"
        if "start" in lowered and "child" not in lowered:
            return "session_start"
        return "lifecycle"
    return "unknown"


def _openclaw_raw_data(data: dict, session_state: dict[str, Any]) -> dict:
    raw = dict(data)
    raw["harness"] = "openclaw"
    raw["event_name"] = str(data.get("type", "unknown"))

    field_aliases = {
        "harness_version": ("harness_version", "harnessVersion", "version"),
        "runtime_id": ("runtime_id", "runtimeId"),
        "runtime_api": ("runtime_api", "runtimeApi"),
        "session_uuid": ("session_uuid", "sessionUuid", "session.id", "sessionId"),
        "session_key": (
            "session_key",
            "sessionKey",
            "routeKey",
            "channelKey",
            "session.key",
        ),
        "parent_session_uuid": (
            "parent_session_uuid",
            "parentSessionUuid",
            "parent_session.id",
            "parent.session.id",
        ),
        "parent_session_key": (
            "parent_session_key",
            "parentSessionKey",
            "parent_session.key",
            "parent.session.key",
        ),
        "child_session_uuid": (
            "child_session_uuid",
            "childSessionUuid",
            "child_session.id",
            "child.session.id",
        ),
        "child_session_key": (
            "child_session_key",
            "childSessionKey",
            "child_session.key",
            "child.session.key",
        ),
        "agent_id": ("agent_id", "agentId", "agent.id"),
        "agent_type": ("agent_type", "agentType", "agent.type"),
        "channel": ("channel",),
        "source_surface": ("source_surface", "sourceSurface"),
        "cwd": ("cwd",),
        "workspace_dir": ("workspace_dir", "workspaceDir"),
        "repository": ("repository", "repo", "remote"),
        "project": ("project",),
        "topic": ("topic", "task_label", "taskLabel"),
        "redaction": ("redaction",),
        "sensitivity": ("sensitivity",),
        "provenance": ("provenance",),
    }

    for dst, aliases in field_aliases.items():
        value = _first_openclaw_value(data, *aliases)
        if value in (None, ""):
            value = session_state.get(dst)
        if value not in (None, ""):
            raw[dst] = value

    if session_state.get("session_id") and not session_state.get(
        "session_uuid_missing"
    ):
        raw["session_uuid"] = session_state["session_id"]
    if session_state.get("session_key"):
        raw["session_key"] = session_state["session_key"]
    if session_state.get("session_uuid_missing"):
        raw["session_uuid_missing"] = True
    return enrich_raw_repo_attribution(raw)


def parse_openclaw_sessions(filepath: str) -> List[AgentEvent]:
    """Parse OpenClaw JSONL session files."""
    events = []

    def new_session_state(
        session_id: str = "unknown_openclaw", uuid_missing: bool = True
    ) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "session_key": None,
            "session_uuid_missing": uuid_missing,
        }

    session_state: dict[str, Any] = new_session_state()
    session_states_by_uuid: dict[str, dict[str, Any]] = {}
    with open(filepath, "r") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            source_event_type = data.get("type")
            session_id, session_key, uuid_missing = _openclaw_session_identity(
                data,
                session_state["session_id"],
                session_state.get("session_key"),
                session_state.get("session_uuid_missing", True),
            )
            if not uuid_missing and session_id != session_state.get("session_id"):
                session_state = session_states_by_uuid.setdefault(
                    session_id, new_session_state(session_id, uuid_missing=False)
                )
            state_updates = {
                "session_id": session_id,
                "session_uuid_missing": uuid_missing,
            }
            if session_key is not None:
                state_updates["session_key"] = session_key
            session_state.update(state_updates)
            if not uuid_missing:
                session_states_by_uuid[session_id] = session_state

            if source_event_type == "session":
                session_aliases = {
                    "cwd": ("cwd", "workspaceDir"),
                    "workspace_dir": ("workspace_dir", "workspaceDir"),
                    "repository": ("repository", "repo", "remote"),
                    "project": ("project",),
                    "topic": ("topic", "task_label", "taskLabel"),
                    "harness_version": ("harness_version", "harnessVersion", "version"),
                    "runtime_id": ("runtime_id", "runtimeId"),
                    "runtime_api": ("runtime_api", "runtimeApi"),
                    "agent_id": ("agent_id", "agentId", "agent.id"),
                    "agent_type": ("agent_type", "agentType", "agent.type"),
                    "channel": ("channel",),
                    "source_surface": ("source_surface", "sourceSurface"),
                    "redaction": ("redaction",),
                    "sensitivity": ("sensitivity",),
                }
                for dst, aliases in session_aliases.items():
                    value = _first_openclaw_value(data, *aliases)
                    if value not in (None, ""):
                        session_state[dst] = value
                continue

            if session_state.get("cwd") and "cwd" not in data:
                data = {**data, "cwd": session_state["cwd"]}

            timestamp_str = data.get("timestamp")
            if timestamp_str:
                timestamp_str = timestamp_str.replace("Z", "+00:00")
                timestamp = datetime.fromisoformat(timestamp_str)
            else:
                timestamp = datetime.now(UTC)

            uuid = data.get("id", f"claw-{timestamp.timestamp()}")
            normalized_type = _normalize_openclaw_event_type(source_event_type, data)

            event = AgentEvent(
                id=uuid,
                session_id=session_id,
                timestamp=timestamp,
                agent_id="nas-openclaw",
                event_type=normalized_type,
                raw_data=_openclaw_raw_data(data, session_state),
            )

            if normalized_type in {"message", "user_turn", "assistant_turn"}:
                msg_data = data.get("message")
                if isinstance(msg_data, dict):
                    role = msg_data.get("role", "unknown")
                    content = msg_data.get("content", "")
                    event.message = Message(role=role, content=content)

            events.append(event)
    return events


_PROV_ATTR_MAP = {
    "prov.harness": "harness",
    "harness": "harness",
    "prov.activity.type": "activity_type",
    "prov.agent.id": "agent_id",
    "prov.agent.type": "agent_type",
    "prov.agent.model": "agent_model",
    "prov.session.key": "session_key",
    "prov.parent.session.id": "parent_session_id",
    "prov.wasAssociatedWith": "associated_with",
    "prov.project": "project",
    "prov.cwd": "cwd",
    "cwd": "cwd",
    "prov.repository": "repository",
    "repository": "repository",
    "prov.git.branch": "branch",
    "prov.repo.owner": "repo_owner",
    "prov.repo.name": "repo_name",
    "prov.task.label": "task_label",
    "prov.llm.provider": "llm_provider",
    "prov.llm.model": "llm_model",
    "prov.llm.stop_reason": "stop_reason",
    "prov.routing.provider": "routing_provider",
    "mux.provider": "routing_provider",
    "mux.selected_provider": "routing_provider",
    "prov.routing.model": "routing_model",
    "mux.model": "routing_model",
    "mux.selected_model": "routing_model",
    "prov.routing.reason": "routing_reason",
    "mux.reason": "routing_reason",
    "mux.fallback_reason": "routing_reason",
    "redaction.level": "redaction_level",
    "sensitivity": "sensitivity",
    "redaction.sensitivity": "sensitivity",
}

_PROJECT_ATTR_KEYS = (
    "prov.project",
    "project",
    "agentweave.project",
    "x-agentweave-project",
    "X-AgentWeave-Project",
)

_INT_ATTR_MAP = {
    "prov.llm.prompt_tokens": "prompt_tokens",
    "prov.llm.completion_tokens": "completion_tokens",
    "prov.llm.total_tokens": "total_tokens",
    "tokens.cache_read": "cache_read_tokens",
    "tokens.cache_write": "cache_write_tokens",
}


def _otel_attr_value(v: dict) -> Any:
    if not v:
        return None
    if "stringValue" in v:
        return v["stringValue"]
    if "intValue" in v:
        # OTel JSON encodes ints as strings; coerce.
        try:
            return int(v["intValue"])
        except (TypeError, ValueError):
            return None
    if "doubleValue" in v:
        return v["doubleValue"]
    if "boolValue" in v:
        return v["boolValue"]
    return None


def _flatten_attrs(attrs: list[dict]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for a in attrs or []:
        k = a.get("key")
        if not k:
            continue
        out[k] = _otel_attr_value(a.get("value", {}))
    return out


def _ns_to_dt(ns: Optional[str]) -> Optional[datetime]:
    if ns in (None, "", "0"):
        return None
    try:
        return datetime.fromtimestamp(int(ns) / 1e9, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


_PREVIEW_BYTES = 2000


def _truncate(s: Optional[str], n: int = _PREVIEW_BYTES) -> Optional[str]:
    if s is None:
        return None
    text = str(s)
    encoded = text.encode("utf-8")
    if len(encoded) <= n:
        return text
    return encoded[:n].decode("utf-8", errors="ignore")


def _truncate_with_flag(
    s: Optional[str], n: int = _PREVIEW_BYTES
) -> tuple[Optional[str], bool]:
    if s is None:
        return None, False
    text = str(s)
    encoded = text.encode("utf-8")
    if len(encoded) <= n:
        return text, False
    return encoded[:n].decode("utf-8", errors="ignore"), True


def _is_preview_attr(key: str) -> bool:
    return key in {
        "prov.llm.prompt_preview",
        "prov.llm.response_preview",
        "prov.tool.input_preview",
        "prov.tool.output_preview",
    } or key.endswith((".tool_input_preview", ".tool_output_preview"))


def _sanitize_preview_attrs(attrs: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    sanitized: dict[str, Any] = dict(attrs)
    truncated = False
    for key, value in attrs.items():
        if _is_preview_attr(key) and isinstance(value, str):
            sanitized[key], value_truncated = _truncate_with_flag(value)
            truncated = truncated or value_truncated
    return sanitized, truncated


def _otel_id_to_hex(s: Optional[str], expected_bytes: int) -> Optional[str]:
    """Normalize an OTel trace/span ID string to lowercase hex.

    Tempo's `/api/traces/<id>?format=json` encodes `traceId` (16 bytes) and
    `spanId` (8 bytes) as base64-encoded bytes per OTel proto-to-JSON rules,
    while `/api/search` returns `traceID` as a hex string. We always store hex.
    """
    if not s:
        return None
    if len(s) == expected_bytes * 2 and all(c in "0123456789abcdefABCDEF" for c in s):
        return s.lower()
    try:
        import base64

        decoded = base64.b64decode(s, validate=False)
        if len(decoded) == expected_bytes:
            return decoded.hex()
        return s
    except Exception:
        return s


def parse_agentweave_trace(trace: dict, raw_object_uri: str) -> list[dict]:
    """Convert a Tempo trace JSON into rows for the lakehouse.spans table.

    `trace` is the parsed body of GET /api/traces/<id>?format=json:
        {"batches": [{"resource": {...}, "scopeSpans": [{"spans": [...]}]}]}

    Spans without a `startTimeUnixNano` are skipped (logged to stderr) because
    the BigQuery `start_time` column is REQUIRED.
    """
    rows: list[dict] = []
    for batch in trace.get("batches", []) or []:
        resource_attrs = _flatten_attrs(
            batch.get("resource", {}).get("attributes", []) or []
        )
        service_name = resource_attrs.get("service.name")
        for ss in (
            batch.get("scopeSpans", [])
            or batch.get("instrumentationLibrarySpans", [])
            or []
        ):
            for span in ss.get("spans", []) or []:
                attrs = _flatten_attrs(span.get("attributes", []) or [])
                # Resource attrs are visible to consumers via attributes_json
                # for completeness; keep span-scoped only as the explicit map.
                merged_for_storage, storage_preview_truncated = _sanitize_preview_attrs(
                    {**resource_attrs, **attrs}
                )

                prompt_preview, prompt_truncated = _truncate_with_flag(
                    attrs.get("prov.llm.prompt_preview")
                )
                response_preview, response_truncated = _truncate_with_flag(
                    attrs.get("prov.llm.response_preview")
                )
                tool_preview_truncated = any(
                    isinstance(value, str)
                    and _is_preview_attr(key)
                    and len(value.encode("utf-8")) > _PREVIEW_BYTES
                    for key, value in attrs.items()
                )

                row: dict = {
                    "trace_id": _otel_id_to_hex(span.get("traceId"), 16),
                    "span_id": _otel_id_to_hex(span.get("spanId"), 8),
                    "parent_span_id": _otel_id_to_hex(
                        span.get("parentSpanId") or None, 8
                    ),
                    "name": span.get("name"),
                    "service_name": service_name,
                    "start_time": _ns_to_dt(span.get("startTimeUnixNano")),
                    "end_time": _ns_to_dt(span.get("endTimeUnixNano")),
                    "session_id": attrs.get("session.id")
                    or attrs.get("prov.session.id"),
                    "prompt_preview": prompt_preview,
                    "response_preview": response_preview,
                    "preview_truncated": bool(
                        prompt_truncated
                        or response_truncated
                        or tool_preview_truncated
                        or storage_preview_truncated
                    ),
                    "preview_bytes": _PREVIEW_BYTES,
                    "cost_usd": attrs.get("cost.usd"),
                    "attributes_json": merged_for_storage,
                    "raw_object_uri": raw_object_uri,
                }
                for src, dst in _PROV_ATTR_MAP.items():
                    value = merged_for_storage.get(src)
                    if dst not in row or row.get(dst) is None:
                        row[dst] = value
                if not row.get("project"):
                    for key in _PROJECT_ATTR_KEYS:
                        project = merged_for_storage.get(key)
                        if project:
                            row["project"] = project
                            break

                attr = derive_repo_attribution(merged_for_storage)
                if row.get("repo_owner") is None and attr.repo_owner:
                    row["repo_owner"] = attr.repo_owner
                if row.get("repo_name") is None and attr.repo_name:
                    row["repo_name"] = attr.repo_name
                if row.get("branch") is None and attr.branch:
                    row["branch"] = attr.branch

                # Reconcile AgentWeave ids to match Lakehouse agent_id schema.
                row["agent_id"] = canonicalize(row.get("agent_id"))
                row["associated_with"] = canonicalize(row.get("associated_with"))

                for src, dst in _INT_ATTR_MAP.items():
                    v = merged_for_storage.get(src)
                    try:
                        row[dst] = int(v) if v is not None else None
                    except (TypeError, ValueError):
                        row[dst] = None
                if row["start_time"] is None:
                    print(
                        f"[parse_agentweave_trace] skipping span without start_time "
                        f"(trace_id={row['trace_id']}, span_id={row['span_id']})",
                        file=sys.stderr,
                    )
                    continue
                if row["start_time"] and row["end_time"]:
                    row["duration_ms"] = (
                        row["end_time"] - row["start_time"]
                    ).total_seconds() * 1000.0
                else:
                    row["duration_ms"] = None
                rows.append(row)
    return rows
