"""Prometheus metrics surface for the Drover runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import http.client
import json
import logging
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping
from urllib.parse import quote, urlencode, urlparse

from drover.server.harness.daemon import (
    _STRUCTURED_DEFAULT_COMMANDS,
    native_transcript_for_session,
)
from drover.server.harness.registry import HarnessRegistry
from drover.server.jobs import RedisJobStream
from drover.server.observatory import pipeline_observatory_snapshot
from drover.server.quality import format_prometheus, quality_snapshot

if TYPE_CHECKING:
    from drover.server.relay_manager import RelayManager

log = logging.getLogger("drover.metrics")

_HARNESS_STALE_AFTER_SECONDS = 45

# Harnesses harnessd can drive as structured sessions (claude-code, codex,
# gemini). A nexus handoff to one of these launches mode="structured" and
# delivers the handoff text as the first turn -- strictly more reliable than
# typing it into a cold PTY (no startup-gate race).
_STRUCTURED_HANDOFF_HARNESSES = frozenset(_STRUCTURED_DEFAULT_COMMANDS)


def _label_value(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(**labels: object) -> str:
    if not labels:
        return ""
    rendered = ",".join(
        f'{key}="{_label_value(value)}"' for key, value in labels.items()
    )
    return f"{{{rendered}}}"


def _metric(name: str, value: object, **labels: object) -> str:
    if isinstance(value, bool):
        metric_value = 1 if value else 0
    elif value is None:
        metric_value = 0
    else:
        metric_value = value
    return f"{name}{_labels(**labels)} {metric_value}"


def _append_details_metrics(lines: list[str], snapshot: dict) -> None:
    summary = (
        snapshot.get("categories", {}).get("summary_coverage", {}).get("details", {})
    )
    embedding = (
        snapshot.get("categories", {}).get("embedding_coverage", {}).get("details", {})
    )
    bundle = snapshot.get("categories", {}).get("bundle_quality", {}).get("details", {})
    span_linkability = (
        snapshot.get("categories", {}).get("span_linkability", {}).get("details", {})
    )
    span_coverage = embedding.get("span_embedding_coverage", {})

    lines.extend(
        [
            "# HELP drover_summary_coverage_percent Percent of event sessions with generated summaries.",
            "# TYPE drover_summary_coverage_percent gauge",
            _metric(
                "drover_summary_coverage_percent",
                summary.get("coverage_percent"),
            ),
            "# HELP drover_event_sessions_without_summary Event sessions missing summaries.",
            "# TYPE drover_event_sessions_without_summary gauge",
            _metric(
                "drover_event_sessions_without_summary",
                summary.get("event_sessions_without_summary"),
            ),
            "# HELP drover_summarize_jobs Jobs in the summarization ledger by status.",
            "# TYPE drover_summarize_jobs gauge",
            _metric(
                "drover_summarize_jobs",
                summary.get("pending_summarize_jobs", 0),
                status="pending",
            ),
            _metric(
                "drover_summarize_jobs",
                summary.get("errored_summarize_jobs", 0),
                status="errored",
            ),
            "# HELP drover_session_embedding_coverage_percent Percent of summaries with session embeddings.",
            "# TYPE drover_session_embedding_coverage_percent gauge",
            _metric(
                "drover_session_embedding_coverage_percent",
                embedding.get("session_embedding_coverage_percent"),
            ),
            "# HELP drover_span_embedding_coverage_percent Percent of spans with semantic embeddings.",
            "# TYPE drover_span_embedding_coverage_percent gauge",
            _metric(
                "drover_span_embedding_coverage_percent",
                span_coverage.get("coverage_percent"),
            ),
            "# HELP drover_bundle_ready_percent Percent of generated summaries ready for handoff bundles.",
            "# TYPE drover_bundle_ready_percent gauge",
            _metric(
                "drover_bundle_ready_percent",
                bundle.get("bundle_ready_percent"),
            ),
            "# HELP drover_openclaw_unmatched_spans OpenClaw/AgentWeave-like spans not linked to native sessions.",
            "# TYPE drover_openclaw_unmatched_spans gauge",
            _metric(
                "drover_openclaw_unmatched_spans",
                span_linkability.get("unmatched_spans", 0),
            ),
        ]
    )


def _append_summarizer_metrics(lines: list[str], report: Mapping[str, Any]) -> None:
    policy = str(report.get("backend_policy") or "unknown")
    allows_anthropic = policy in {"hybrid", "cloud"}
    allows_local = policy in {"hybrid", "local"}
    lines.extend(
        [
            "# HELP drover_summarizer_policy Current summarizer backend policy.",
            "# TYPE drover_summarizer_policy gauge",
        ]
    )
    for candidate in ("hybrid", "cloud", "local", "unknown"):
        lines.append(
            _metric(
                "drover_summarizer_policy",
                1 if candidate == policy else 0,
                policy=candidate,
            )
        )
    lines.extend(
        [
            "# HELP drover_summarizer_backend_ready Whether each summarizer backend is configured and usable.",
            "# TYPE drover_summarizer_backend_ready gauge",
            _metric(
                "drover_summarizer_backend_ready",
                bool(report.get("anthropic_ready")),
                backend="anthropic",
            ),
            _metric(
                "drover_summarizer_backend_ready",
                bool(report.get("local_ready")),
                backend="local",
            ),
            "# HELP drover_summarizer_backend_allowed Whether policy allows each summarizer backend.",
            "# TYPE drover_summarizer_backend_allowed gauge",
            _metric(
                "drover_summarizer_backend_allowed",
                allows_anthropic,
                backend="anthropic",
            ),
            _metric(
                "drover_summarizer_backend_allowed",
                allows_local,
                backend="local",
            ),
        ]
    )


def _append_redis_metrics(
    lines: list[str], job_streams: Mapping[str, RedisJobStream]
) -> None:
    lines.extend(
        [
            "# HELP drover_redis_job_stream_length Redis Stream length by Drover derived queue.",
            "# TYPE drover_redis_job_stream_length gauge",
            "# HELP drover_redis_job_stream_pending Redis consumer-group pending entries by Drover derived queue.",
            "# TYPE drover_redis_job_stream_pending gauge",
            "# HELP drover_redis_job_stream_undelivered Redis Stream undelivered entries by Drover derived queue.",
            "# TYPE drover_redis_job_stream_undelivered gauge",
            "# HELP drover_redis_job_stream_dead Redis dead-letter entries by Drover derived queue.",
            "# TYPE drover_redis_job_stream_dead gauge",
            "# HELP drover_redis_job_stream_should_shed Whether Redis queue backlog is past the configured high-water mark.",
            "# TYPE drover_redis_job_stream_should_shed gauge",
            "# HELP drover_redis_job_stream_high_water Redis queue high-water mark.",
            "# TYPE drover_redis_job_stream_high_water gauge",
            "# HELP drover_redis_job_stream_scrape_error Whether Redis stream metrics failed to scrape.",
            "# TYPE drover_redis_job_stream_scrape_error gauge",
        ]
    )
    for queue, stream in sorted(job_streams.items()):
        try:
            backpressure = stream.backpressure()
            lines.extend(
                [
                    _metric(
                        "drover_redis_job_stream_length", stream.length(), queue=queue
                    ),
                    _metric(
                        "drover_redis_job_stream_pending",
                        backpressure.get("pending", 0),
                        queue=queue,
                    ),
                    _metric(
                        "drover_redis_job_stream_undelivered",
                        backpressure.get("undelivered", 0),
                        queue=queue,
                    ),
                    _metric(
                        "drover_redis_job_stream_dead",
                        backpressure.get("dead", 0),
                        queue=queue,
                    ),
                    _metric(
                        "drover_redis_job_stream_should_shed",
                        backpressure.get("should_shed", False),
                        queue=queue,
                    ),
                    _metric(
                        "drover_redis_job_stream_high_water",
                        backpressure.get("high_water", 0),
                        queue=queue,
                    ),
                    _metric("drover_redis_job_stream_scrape_error", 0, queue=queue),
                ]
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("failed to scrape Redis stream metrics for %s: %s", queue, exc)
            lines.append(
                _metric("drover_redis_job_stream_scrape_error", 1, queue=queue)
            )


def _redis_stream_snapshots(
    job_streams: Mapping[str, RedisJobStream],
) -> dict[str, dict[str, Any]]:
    snapshots: dict[str, dict[str, Any]] = {}
    for queue, stream in sorted(job_streams.items()):
        try:
            length = int(stream.length())
            backpressure = stream.backpressure()
            snapshots[queue] = {
                "stream": getattr(stream, "name", queue),
                "length": length,
                "backlog": int(backpressure.get("backlog", 0) or 0),
                "pending": int(backpressure.get("pending", 0) or 0),
                "undelivered": int(backpressure.get("undelivered", 0) or 0),
                "dead": int(backpressure.get("dead", 0) or 0),
                "high_water": int(backpressure.get("high_water", 0) or 0),
                "should_shed": bool(backpressure.get("should_shed", False)),
            }
        except Exception as exc:  # noqa: BLE001
            log.warning("failed to render Redis stream snapshot for %s: %s", queue, exc)
            snapshots[queue] = {"error": str(exc)}
    return snapshots


def _harness_cwd_suggestions(
    sessions: list[Any], favorite_cwds: tuple[str, ...] = ()
) -> list[dict[str, str]]:
    suggestions: list[dict[str, str]] = []
    seen: set[tuple[str | None, str]] = set()

    def add(path: str | None, *, source: str, host_id: str | None = None) -> None:
        path = (path or "").strip()
        if not path:
            return
        key = (host_id, path)
        if key in seen:
            return
        seen.add(key)
        item = {"path": path, "source": source}
        if host_id:
            item["host_id"] = host_id
        suggestions.append(item)

    for session in sessions:
        add(
            getattr(session, "cwd", None),
            source="recent session",
            host_id=getattr(session, "host_id", None),
        )
    for path in favorite_cwds:
        add(path, source="favorite")
    return suggestions[:24]


def _harness_endpoint(host: Any) -> str:
    return (
        getattr(host, "local_url", None) or getattr(host, "tailscale_url", None) or ""
    ).rstrip("/")


def _json_response(status: int, payload: Mapping[str, Any]) -> tuple[int, str]:
    return status, json.dumps(dict(payload), sort_keys=True, default=str) + "\n"


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_event_timestamp(value: Any) -> datetime | None:
    text = _optional_str(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _command_label(command: Any) -> str:
    if isinstance(command, str):
        return command
    if isinstance(command, list | tuple):
        return " ".join(str(part) for part in command)
    return str(command)


def _native_session_id(native_resume: Any) -> str | None:
    if not isinstance(native_resume, dict):
        return None
    return _optional_str(native_resume.get("session_id"))


def _native_resume_label(native_resume: Any) -> str | None:
    if not isinstance(native_resume, dict):
        return None
    if label := _optional_str(native_resume.get("label")):
        return label
    if session_id := _optional_str(native_resume.get("session_id")):
        return session_id
    if native_resume.get("latest"):
        return "latest"
    return _optional_str(native_resume.get("mode"))


def _harness_host_dict(host: Any) -> dict[str, Any]:
    item = dict(host.__dict__)
    last_seen_at = getattr(host, "last_seen_at", None)
    if last_seen_at is None:
        return item
    if last_seen_at.tzinfo is None:
        age_s = (datetime.now() - last_seen_at).total_seconds()
    else:
        age_s = (datetime.now(timezone.utc) - last_seen_at).total_seconds()
    if age_s > _HARNESS_STALE_AFTER_SECONDS and item.get("status") == "online":
        item["status"] = "stale"
        item["stale_after_seconds"] = _HARNESS_STALE_AFTER_SECONDS
    return item


def _append_adoption_metrics(lines: list[str], snapshot: dict) -> None:
    adoption = (
        snapshot.get("categories", {}).get("agent_adoption", {}).get("details", {})
    )
    runtimes = adoption.get("runtimes", []) or []
    lines.extend(
        [
            "# HELP drover_agent_adoption_ready Whether an agent runtime has Drover events, MCP, and skill coverage.",
            "# TYPE drover_agent_adoption_ready gauge",
            "# HELP drover_agent_adoption_observed_events Observed event volume by registered agent runtime.",
            "# TYPE drover_agent_adoption_observed_events gauge",
            "# HELP drover_agent_adoption_unmatched_high_volume_agents High-volume observed agents missing from the adoption registry.",
            "# TYPE drover_agent_adoption_unmatched_high_volume_agents gauge",
        ]
    )
    for row in runtimes:
        runtime = row.get("runtime", "unknown")
        lines.append(
            _metric(
                "drover_agent_adoption_ready",
                bool(row.get("ready")),
                runtime=runtime,
                status=row.get("status", "unknown"),
            )
        )
        lines.append(
            _metric(
                "drover_agent_adoption_observed_events",
                int(row.get("observed_events", 0) or 0),
                runtime=runtime,
            )
        )
    lines.append(
        _metric(
            "drover_agent_adoption_unmatched_high_volume_agents",
            len(adoption.get("unmatched_high_volume_agent_ids", []) or []),
        )
    )


@dataclass
class MetricsCollector:
    """Cached Drover metrics renderer for Prometheus scrapes."""

    duckdb_path: Path
    incoming_dir: Path
    summarizer_report: Mapping[str, Any]
    job_streams: Mapping[str, RedisJobStream] = field(default_factory=dict)
    ttl_seconds: float = 60.0
    api_token: str = ""
    # New Session sheet "favorite" cwd suggestions, from [harness].favorite_cwds
    # in ~/.drover/config.toml (empty by default — no hardcoded paths).
    favorite_cwds: tuple[str, ...] = ()
    # Set by start_metrics_server; owns live hub<->harnessd relay connections.
    relay_manager: "RelayManager | None" = None
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _cached_text: str | None = field(default=None, init=False)
    _cached_json: str | None = field(default=None, init=False)
    _cached_until: float = field(default=0.0, init=False)

    def render_prometheus(self) -> str:
        self._refresh_if_needed()
        return self._cached_text or ""

    def render_json(self) -> str:
        self._refresh_if_needed()
        return self._cached_json or "{}\n"

    def render_harness_json(
        self,
        *,
        include_hosts: bool = True,
        include_sessions: bool = True,
    ) -> str:
        snapshot = self.harness_snapshot(
            include_hosts=include_hosts,
            include_sessions=include_sessions,
        )
        return json.dumps(snapshot, sort_keys=True, default=str) + "\n"

    def render_harness_session_json(self, session_id: str) -> tuple[int, str]:
        snapshot = self.harness_session_snapshot(session_id)
        status = 404 if snapshot.get("error") else 200
        return status, json.dumps(snapshot, sort_keys=True, default=str) + "\n"

    def register_harness_host(self, payload: Mapping[str, Any]) -> tuple[int, str]:
        host_id = str(payload.get("host_id") or "").strip()
        if not host_id:
            return _json_response(400, {"error": "missing host_id"})
        display_name = str(payload.get("display_name") or host_id)
        kind = str(payload.get("kind") or "unknown")
        status = str(payload.get("status") or "online")
        capabilities = payload.get("capabilities")
        if capabilities is not None and not isinstance(capabilities, dict):
            return _json_response(400, {"error": "capabilities must be an object"})
        try:
            registry = HarnessRegistry(self.duckdb_path)
            host = registry.register_host(
                host_id=host_id,
                display_name=display_name,
                kind=kind,
                local_url=_optional_str(payload.get("local_url")),
                tailscale_url=_optional_str(payload.get("tailscale_url")),
                connection_kind=str(payload.get("connection_kind") or "direct"),
                status=status,
                capabilities=capabilities,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("failed to register harness host %s: %s", host_id, exc)
            return _json_response(500, {"error": str(exc)})
        return _json_response(200, {"host": host.__dict__})

    def proxy_create_harness_session(
        self, host_id: str, payload: Mapping[str, Any]
    ) -> tuple[int, str]:
        host = self._harness_host(host_id)
        if host is None:
            return _json_response(404, {"error": f"unknown harness host: {host_id}"})
        endpoint = _harness_endpoint(host)
        if not endpoint:
            return _json_response(
                502, {"error": f"harness host has no registered endpoint: {host_id}"}
            )
        status, body = self._proxy_harness_request(
            f"{endpoint}/sessions",
            method="POST",
            payload=payload,
        )
        if 200 <= status < 300:
            self._sync_created_harness_session(host_id, payload, body)
        return status, body

    def proxy_terminate_harness_session(self, session_id: str) -> tuple[int, str]:
        session = self._harness_session(session_id)
        if session is None:
            return _json_response(
                404, {"error": f"unknown harness session: {session_id}"}
            )
        host = self._harness_host(session.host_id)
        if host is None:
            return _json_response(
                404, {"error": f"unknown harness host: {session.host_id}"}
            )
        endpoint = _harness_endpoint(host)
        if not endpoint:
            return _json_response(
                502,
                {
                    "error": f"harness host has no registered endpoint: {session.host_id}"
                },
            )
        status, body = self._proxy_harness_request(
            f"{endpoint}/sessions/{session_id}/terminate",
            method="POST",
            payload={},
        )
        if 200 <= status < 300:
            self._sync_terminated_harness_session(session_id, body)
            return status, body
        if status in (404, 502):
            # The daemon no longer knows this session (restart lost it) or
            # the host is unreachable entirely. Either way there is nothing
            # left to terminate on the host -- tombstone the registry row so
            # it stops looking alive, and report success to the client.
            self._tombstone_stale_harness_session(session_id)
            return _json_response(
                200,
                {
                    "session_id": session_id,
                    "status": "terminated",
                    "stale": True,
                },
            )
        return status, body

    def proxy_harness_session_action(
        self,
        session_id: str,
        action: str,
        payload: Mapping[str, Any],
    ) -> tuple[int, str]:
        """Proxy a structured-session action (turns/permission/interrupt) to
        the owning host's harnessd, forwarding the JSON body verbatim."""
        session = self._harness_session(session_id)
        if session is None:
            return _json_response(
                404, {"error": f"unknown harness session: {session_id}"}
            )
        host = self._harness_host(session.host_id)
        if host is None:
            return _json_response(
                404, {"error": f"unknown harness host: {session.host_id}"}
            )
        endpoint = _harness_endpoint(host)
        if not endpoint:
            return _json_response(
                502,
                {
                    "error": f"harness host has no registered endpoint: {session.host_id}"
                },
            )
        return self._proxy_harness_request(
            f"{endpoint}/sessions/{session_id}/{action}",
            method="POST",
            payload=payload,
        )

    def proxy_harness_native_sessions(
        self,
        host_id: str,
        query: Mapping[str, Any],
    ) -> tuple[int, str]:
        host = self._harness_host(host_id)
        if host is None:
            return _json_response(404, {"error": f"unknown harness host: {host_id}"})
        endpoint = _harness_endpoint(host)
        if not endpoint:
            return _json_response(
                502, {"error": f"harness host has no registered endpoint: {host_id}"}
            )
        params = {
            key: value
            for key, value in {
                "harness": query.get("harness"),
                "cwd": query.get("cwd"),
                "limit": query.get("limit") or 20,
            }.items()
            if value not in {None, ""}
        }
        path = "/native-sessions"
        if params:
            path = f"{path}?{urlencode(params)}"
        return self._proxy_harness_request(
            f"{endpoint}{path}",
            method="GET",
            payload={},
        )

    def proxy_harness_auth(
        self,
        host_id: str,
        harness: str,
        action: str,
        *,
        flow_id: str | None = None,
    ) -> tuple[int, str]:
        host = self._harness_host(host_id)
        if host is None:
            return _json_response(404, {"error": f"unknown harness host: {host_id}"})
        endpoint = _harness_endpoint(host)
        if not endpoint:
            return _json_response(
                502, {"error": f"harness host has no registered endpoint: {host_id}"}
            )

        if action in {"status", "start"}:
            path = f"/auth/{quote(harness, safe='')}/{action}"
        elif action in {"flow", "cancel"} and flow_id:
            suffix = "" if action == "flow" else "/cancel"
            path = f"/auth/{quote(harness, safe='')}/flows/{quote(flow_id, safe='')}{suffix}"
        else:
            return _json_response(400, {"error": "invalid auth action"})

        status, body = self._proxy_harness_request(
            f"{endpoint}{path}",
            method="GET" if action in {"status", "flow"} else "POST",
            payload={},
        )
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return status, body
        if isinstance(payload, dict):
            payload.setdefault("host_id", host_id)
            payload.setdefault("harness", harness)
            return _json_response(status, payload)
        return status, body

    def proxy_harness_native_transcript(self, session_id: str) -> tuple[int, str]:
        session = self._harness_session(session_id)
        if session is None:
            return _json_response(
                404, {"error": f"unknown harness session: {session_id}"}
            )
        host = self._harness_host(session.host_id)
        if host is None:
            return _json_response(
                404, {"error": f"unknown harness host: {session.host_id}"}
            )
        endpoint = _harness_endpoint(host)
        if not endpoint:
            return _json_response(
                502,
                {
                    "error": f"harness host has no registered endpoint: {session.host_id}"
                },
            )
        params = {
            key: value
            for key, value in {
                "native_session_id": session.native_session_id,
                "limit": 100,
            }.items()
            if value not in {None, ""}
        }
        path = f"/sessions/{session_id}/native-transcript"
        if params:
            path = f"{path}?{urlencode(params)}"
        status, body = self._proxy_harness_request(
            f"{endpoint}{path}",
            method="GET",
            payload={},
            timeout_s=2.0,
        )
        if status == 404 and session.host_id in {"mac-mini", "localhost"}:
            transcript = native_transcript_for_session(
                harness=session.harness,
                cwd=session.cwd,
                native_session_id=session.native_session_id,
                limit=100,
            )
            provider_session_id = transcript.get("session_id")
            transcript.update(
                {
                    "host_id": session.host_id,
                    "session_id": session_id,
                    "native_session_id": provider_session_id,
                    "harness": session.harness,
                    "cwd": session.cwd,
                }
            )
            return _json_response(200, transcript)
        return status, body

    def continue_harness_session(
        self,
        session_id: str,
        payload: Mapping[str, Any],
    ) -> tuple[int, str]:
        source = self._harness_session(session_id)
        if source is None:
            return _json_response(
                404, {"error": f"unknown harness session: {session_id}"}
            )
        target_host_id = str(payload.get("target_host_id") or source.host_id)
        target_harness = str(payload.get("target_harness") or source.harness)
        native_resume = payload.get("native_resume")
        handoff_mode = "native_resume" if native_resume else "nexus_handoff"
        if not native_resume and target_harness in _STRUCTURED_HANDOFF_HARNESSES:
            # Nexus handoff to a structured-capable harness: launch a
            # structured session and deliver the handoff text as the first
            # turn ("prompt"). The daemon sends it once the driver is up, so
            # there is no typed-seed race against the CLI's cold start (and
            # no rows/cols/initial_input -- those are PTY concepts).
            structured_payload: dict[str, Any] = {
                "mode": "structured",
                "harness": target_harness,
                "cwd": source.cwd,
                "repo_owner": source.repo_owner,
                "repo_name": source.repo_name,
                "branch": source.branch,
                "source_session_id": source.session_id,
                "handoff_mode": "nexus_handoff",
                "prompt": self._build_handoff_prompt(
                    source,
                    target_harness=target_harness,
                ),
            }
            return self.proxy_create_harness_session(
                target_host_id, structured_payload
            )
        launch_payload: dict[str, Any] = {
            "harness": target_harness,
            "cwd": source.cwd,
            "repo_owner": source.repo_owner,
            "repo_name": source.repo_name,
            "branch": source.branch,
            "source_session_id": source.session_id,
            "handoff_mode": handoff_mode,
            "native_resume": native_resume,
            "rows": payload.get("rows") or 32,
            "cols": payload.get("cols") or 100,
        }
        if not native_resume or target_harness != source.harness:
            launch_payload["initial_input"] = self._build_handoff_prompt(
                source,
                target_harness=target_harness,
            )
            launch_payload["handoff_mode"] = "nexus_handoff"
        return self.proxy_create_harness_session(target_host_id, launch_payload)

    def harness_terminal_endpoint(self, session_id: str) -> str | None:
        if not self._reconcile_harness_session_from_host(session_id):
            return None
        session = self._harness_session(session_id)
        if session is None:
            return None
        if str(session.status) not in {"created", "starting", "running"}:
            return None
        host = self._harness_host(session.host_id)
        if host is None:
            return None
        endpoint = _harness_endpoint(host)
        if not endpoint:
            return None
        return f"{endpoint}/sessions/{session_id}/terminal"

    def _reconcile_harness_session_from_host(self, session_id: str) -> bool:
        session = self._harness_session(session_id)
        if session is None:
            return False
        if str(session.status) not in {"created", "starting", "running"}:
            return True
        host = self._harness_host(session.host_id)
        if host is None:
            return True
        endpoint = _harness_endpoint(host)
        if not endpoint:
            return True
        status, body = self._proxy_harness_request(
            f"{endpoint}/sessions/{session_id}",
            method="GET",
            payload={},
            timeout_s=1.0,
        )
        if 200 <= status < 300:
            return True
        if status == 404:
            self._mark_harness_session_missing_on_host(session_id)
            return False
        log.warning(
            "failed to reconcile harness session %s from %s: %s %s",
            session_id,
            endpoint,
            status,
            body.strip()[:300],
        )
        return True

    def _tombstone_stale_harness_session(self, session_id: str) -> None:
        # Mirrors _sync_terminated_harness_session's terminal status so the
        # row buckets as done on clients, but records why the daemon never
        # acknowledged the terminate.
        try:
            HarnessRegistry(self.duckdb_path).update_session_status(
                session_id,
                "terminated",
                ended_at=datetime.now(timezone.utc),
                last_error="session missing on host; tombstoned by central terminate",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "failed to tombstone stale harness session %s: %s", session_id, exc
            )

    def _mark_harness_session_missing_on_host(self, session_id: str) -> None:
        try:
            HarnessRegistry(self.duckdb_path).update_session_status(
                session_id,
                "completed",
                ended_at=datetime.now(timezone.utc),
                last_error="host no longer has active PTY session; reconciled before terminal attach",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("failed to mark stale harness session %s: %s", session_id, exc)

    def harness_snapshot(
        self,
        *,
        include_hosts: bool = True,
        include_sessions: bool = True,
    ) -> dict[str, Any]:
        source = Path(self.duckdb_path)
        if not source.exists():
            return {
                "hosts": [],
                "sessions": [],
                "error": f"DuckDB file does not exist: {source}",
            }
        try:
            with tempfile.TemporaryDirectory(prefix="drover-harness-") as tmp:
                snapshot = Path(tmp) / source.name
                shutil.copy2(source, snapshot)
                registry = HarnessRegistry(snapshot)
                hosts = registry.list_hosts() if include_hosts else []
                sessions = registry.list_sessions() if include_sessions else []
                return {
                    "hosts": [_harness_host_dict(host) for host in hosts],
                    "sessions": [session.__dict__ for session in sessions],
                    "cwd_suggestions": _harness_cwd_suggestions(
                        sessions, self.favorite_cwds
                    ),
                }
        except Exception as exc:  # noqa: BLE001
            log.warning("failed to render harness snapshot: %s", exc)
            return {"hosts": [], "sessions": [], "error": str(exc)}

    def harness_session_snapshot(self, session_id: str) -> dict[str, Any]:
        source = Path(self.duckdb_path)
        if not source.exists():
            return {"error": f"DuckDB file does not exist: {source}"}
        try:
            self._reconcile_harness_session_from_host(session_id)
            with tempfile.TemporaryDirectory(prefix="drover-harness-session-") as tmp:
                snapshot = Path(tmp) / source.name
                shutil.copy2(source, snapshot)
                registry = HarnessRegistry(snapshot)
                session = registry.get_session(session_id)
                if session is None:
                    return {"error": f"unknown harness session: {session_id}"}
                host = registry.get_host(session.host_id)
                chunks = registry.list_transcript_chunks(session_id)
                events = registry.list_events(session_id)
                native_transcript: dict[str, Any] | None = None
                status, body = self.proxy_harness_native_transcript(session_id)
                if 200 <= status < 300:
                    try:
                        parsed = json.loads(body)
                    except json.JSONDecodeError:
                        parsed = {}
                    if isinstance(parsed, dict):
                        native_transcript = parsed
                return {
                    "session": session.__dict__,
                    "host": host.__dict__ if host else None,
                    "events": [event.__dict__ for event in events],
                    "transcript_chunks": [chunk.__dict__ for chunk in chunks],
                    "native_transcript": native_transcript,
                }
        except Exception as exc:  # noqa: BLE001
            log.warning("failed to render harness session %s: %s", session_id, exc)
            return {"error": str(exc)}

    def _harness_host(self, host_id: str) -> Any | None:
        try:
            registry = HarnessRegistry(self.duckdb_path)
            return registry.get_host(host_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("failed to load harness host %s: %s", host_id, exc)
            return None

    def _harness_session(self, session_id: str) -> Any | None:
        try:
            registry = HarnessRegistry(self.duckdb_path)
            return registry.get_session(session_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("failed to load harness session %s: %s", session_id, exc)
            return None

    def _sync_created_harness_session(
        self,
        host_id: str,
        request_payload: Mapping[str, Any],
        response_body: str,
    ) -> None:
        try:
            payload = json.loads(response_body)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return
        session_id = str(payload.get("session_id") or "").strip()
        if not session_id:
            return
        harness = str(
            payload.get("harness") or request_payload.get("harness") or "shell"
        )
        command = payload.get("command") or request_payload.get("command") or harness
        status = str(payload.get("status") or "running")
        mode = str(payload.get("mode") or request_payload.get("mode") or "pty")
        registry = HarnessRegistry(self.duckdb_path)
        try:
            existing = registry.get_session(session_id)
            if existing is None:
                registry.create_session(
                    session_id=session_id,
                    host_id=host_id,
                    harness=harness,
                    mode=mode,
                    command=_command_label(command),
                    repo_owner=_optional_str(request_payload.get("repo_owner")),
                    repo_name=_optional_str(request_payload.get("repo_name")),
                    branch=_optional_str(request_payload.get("branch")),
                    cwd=_optional_str(request_payload.get("cwd")),
                    status=status,
                    native_session_id=_native_session_id(
                        request_payload.get("native_resume")
                    ),
                    native_resume_label=_native_resume_label(
                        request_payload.get("native_resume")
                    ),
                    source_session_id=_optional_str(
                        request_payload.get("source_session_id")
                    ),
                    handoff_mode=_optional_str(request_payload.get("handoff_mode")),
                )
            else:
                registry.update_session_status(session_id, status)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "failed to sync created harness session %s: %s", session_id, exc
            )

    def _sync_terminated_harness_session(
        self,
        session_id: str,
        response_body: str,
    ) -> None:
        status = "terminated"
        try:
            payload = json.loads(response_body)
            if isinstance(payload, dict):
                status = str(payload.get("status") or status)
        except json.JSONDecodeError:
            pass
        try:
            HarnessRegistry(self.duckdb_path).update_session_status(
                session_id,
                status,
                ended_at=datetime.now(timezone.utc),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "failed to sync terminated harness session %s: %s", session_id, exc
            )

    def _build_handoff_prompt(self, source: Any, *, target_harness: str) -> str:
        chunks = []
        try:
            registry = HarnessRegistry(self.duckdb_path)
            chunks = registry.list_transcript_chunks(source.session_id)[-8:]
        except Exception:
            chunks = []
        transcript = "\n".join(
            chunk.content_redacted for chunk in chunks if chunk.content_redacted
        ).strip()
        if len(transcript) > 4000:
            transcript = transcript[-4000:]
        lines = [
            "Continue this Drover Harness session in the current CLI.",
            "",
            f"Source session: {source.session_id}",
            f"Source harness: {source.harness}",
            f"Target harness: {target_harness}",
            f"Host: {source.host_id}",
            f"CWD: {source.cwd or '(not recorded)'}",
            f"Command: {source.command}",
            f"Status at handoff: {source.status}",
            "",
            "Use the context below to continue the work without asking me to restate it.",
        ]
        if transcript:
            lines.extend(["", "Recent transcript:", transcript])
        else:
            lines.extend(
                ["", "Recent transcript: not available in central Drover yet."]
            )
        lines.extend(["", "Start by briefly confirming what you are continuing."])
        return "\n".join(lines).strip() + "\n"

    def _proxy_harness_request(
        self,
        url: str,
        *,
        method: str,
        payload: Mapping[str, Any] | None = None,
        timeout_s: float = 15,
    ) -> tuple[int, str]:
        body = json.dumps(dict(payload or {}), sort_keys=True)
        parsed = urlparse(url)
        if parsed.scheme != "http" or not parsed.hostname:
            return _json_response(502, {"error": f"unsupported harness URL: {url}"})
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        port = parsed.port or 80
        try:
            conn = http.client.HTTPConnection(parsed.hostname, port, timeout=timeout_s)
            headers = {"Content-Type": "application/json"}
            if self.api_token:
                headers["Authorization"] = f"Bearer {self.api_token}"
            conn.request(
                method,
                path,
                body=body.encode("utf-8") if method != "GET" else None,
                headers=headers,
            )
            response = conn.getresponse()
            return response.status, response.read().decode("utf-8")
        except OSError as exc:
            return _json_response(502, {"error": f"harness host request failed: {exc}"})
        except Exception as exc:  # noqa: BLE001
            log.warning("failed to proxy harness request to %s: %s", url, exc)
            return _json_response(502, {"error": str(exc)})
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _refresh_if_needed(self) -> None:
        now = time.monotonic()
        if self._cached_text is not None and now < self._cached_until:
            return
        with self._lock:
            now = time.monotonic()
            if self._cached_text is not None and now < self._cached_until:
                return
            snapshot = self._quality_snapshot()
            lines = [format_prometheus(snapshot).rstrip()]
            _append_details_metrics(lines, snapshot)
            _append_summarizer_metrics(lines, self.summarizer_report)
            _append_redis_metrics(lines, self.job_streams)
            _append_adoption_metrics(lines, snapshot)
            observatory = self._observatory_snapshot(snapshot)
            redis_streams = _redis_stream_snapshots(self.job_streams)
            self._cached_text = "\n".join(lines) + "\n"
            self._cached_json = (
                json.dumps(
                    {
                        "quality": snapshot,
                        "observatory": observatory,
                        "redis_streams": redis_streams,
                        "summarizer": dict(self.summarizer_report),
                        "redis_queues": sorted(self.job_streams),
                    },
                    sort_keys=True,
                    default=str,
                )
                + "\n"
            )
            self._cached_until = now + self.ttl_seconds

    def _quality_snapshot(self) -> dict:
        source = Path(self.duckdb_path)
        if not source.exists():
            return quality_snapshot(
                duckdb_path=source,
                incoming_dir=self.incoming_dir,
                deep=False,
            )
        with tempfile.TemporaryDirectory(prefix="drover-metrics-") as tmp:
            snapshot = Path(tmp) / source.name
            shutil.copy2(source, snapshot)
            return quality_snapshot(
                duckdb_path=snapshot,
                incoming_dir=self.incoming_dir,
                deep=False,
            )

    def _observatory_snapshot(self, quality: dict) -> dict:
        audit = quality.get("runtime_audit", {})
        source = Path(self.duckdb_path)
        if not source.exists():
            return {}
        try:
            with tempfile.TemporaryDirectory(prefix="drover-observatory-") as tmp:
                snapshot = Path(tmp) / source.name
                shutil.copy2(source, snapshot)
                return pipeline_observatory_snapshot(
                    duckdb_path=snapshot,
                    runtime_audit=audit,
                    max_artifacts=10,
                    max_projects=10,
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("failed to render observatory drilldown: %s", exc)
            return {"error": str(exc)}


from drover.server.web.app import (
    start_metrics_server,
)  # noqa: E402,F401 - compat re-export
