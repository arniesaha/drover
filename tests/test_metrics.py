from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import logging
from pathlib import Path
import base64
import gzip
import os
import duckdb
import shutil
import socket
import threading
from time import monotonic
import urllib.error
import urllib.request
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

import drover.server.harness.registry as registry_module
from drover.config import AdvisoryContentConfig
from drover.schema import bootstrap
from drover.server import metrics
from drover.server.advisory.repository import AdvisoryRepository
from drover.server.advisory.service import InsightsService
from drover.server.advisory.types import (
    AnalyzerClass,
    Confidence,
    FindingCandidate,
    FindingEvidence,
    Severity,
)
from drover.server.cockpit.service import CockpitService, ProviderRefreshLoop
from drover.server.db import control_plane_path
from drover.server.harness.daemon import (
    DEFAULT_PRESETS,
    HarnessDaemonState,
    create_harness_server,
    register_daemon_host,
)
from drover.server.harness.content_consent import DurableContentConsent
from drover.server.harness.pty import PtySessionManager
from drover.server.harness.recap_worker import LiveRecapWorker
from drover.server.harness.registry import HarnessRegistry
from drover.server.harness.websocket import (
    OPCODE_PING,
    OPCODE_PONG,
    client_handshake,
    client_send_frame,
    client_send_json,
    recv_frame,
)
from drover.server.metrics import MetricsCollector, start_metrics_server
from drover.server.web.app import _MetricsHandler
from drover.server.web.auth import AuthSettings, mint_session
from drover.server.web.ui import load_page


def _snapshot() -> dict:
    return {
        "score": 0.85,
        "status": "warn",
        "warnings": ["bundle_quality: partial"],
        "categories": {
            "freshness": {
                "score": 1.0,
                "status": "ok",
                "details": {
                    "latest_event_age_hours_by_agent": {},
                    "latest_span_age_hours": 0.1,
                    "unprocessed_incoming_files": 0,
                },
            },
            "attribution": {
                "score": 1.0,
                "status": "ok",
                "details": {"repo_attribution_percent_by_agent": {}},
            },
            "identity": {
                "score": 1.0,
                "status": "ok",
                "details": {
                    "duplicate_id_values": 0,
                    "duplicate_dedup_key_values": 0,
                },
            },
            "derived_context": {
                "score": 1.0,
                "status": "ok",
                "details": {"handoff_ready": True},
            },
            "summary_coverage": {
                "score": 1.0,
                "status": "ok",
                "details": {
                    "coverage_percent": 100.0,
                    "event_sessions_without_summary": 0,
                    "pending_summarize_jobs": 0,
                    "errored_summarize_jobs": 0,
                },
            },
            "embedding_coverage": {
                "score": 1.0,
                "status": "ok",
                "details": {
                    "session_embedding_coverage_percent": 100.0,
                    "span_embedding_coverage": {"coverage_percent": 99.5},
                },
            },
            "bundle_quality": {
                "score": 0.5,
                "status": "warn",
                "details": {"bundle_ready_percent": 64.2},
            },
            "span_linkability": {
                "score": 0.5,
                "status": "warn",
                "details": {"unmatched_spans": 5},
            },
            "agent_adoption": {
                "score": 1.0,
                "status": "ok",
                "details": {
                    "runtimes": [
                        {
                            "runtime": "openclaw-main",
                            "ready": True,
                            "status": "active",
                            "observed_events": 12,
                        }
                    ],
                    "unmatched_high_volume_agent_ids": [],
                },
            },
        },
        "runtime_audit": {"table_counts": {"agent_events": 10}},
    }


class _Stream:
    def length(self) -> int:
        return 4

    def backpressure(self) -> dict:
        return {
            "pending": 1,
            "undelivered": 3,
            "dead": 0,
            "should_shed": False,
            "high_water": 500,
        }


class _FakeHarnessHandler(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def do_GET(self) -> None:  # noqa: N802
        self.__class__.requests.append(
            {
                "path": self.path,
                "body": None,
                "authorization": self.headers.get("Authorization"),
            }
        )
        if self.path.startswith("/native-sessions"):
            response = {
                "host_id": "mac-mini",
                "sessions": [
                    {
                        "harness": "codex",
                        "session_id": "codex-native-1",
                        "label": "nexus · codex-na",
                        "cwd": "/tmp/nexus",
                        "native_resume": {
                            "session_id": "codex-native-1",
                            "label": "nexus · codex-na",
                        },
                    }
                ],
            }
        elif self.path.startswith("/sessions/harness-running/native-transcript"):
            response = {
                "host_id": "mac-mini",
                "session_id": "harness-running",
                "source": "claude jsonl",
                "native_session_id": "claude-native-1",
                "messages": [
                    {
                        "role": "user",
                        "text": "Summarise this project.",
                        "created_at": "2026-06-29T05:00:00Z",
                    },
                    {
                        "role": "assistant",
                        "text": "Nexus is a local-first context store.",
                        "created_at": "2026-06-29T05:00:01Z",
                    },
                ],
            }
        elif self.path == "/sessions/harness-running":
            response = {
                "session_id": "harness-running",
                "command": ["/bin/sh"],
                "cwd": "/tmp/nexus",
                "pid": 123,
                "status": "running",
            }
        else:
            self.send_response(404)
            self.end_headers()
            return
        payload = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or "0")
        body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        self.__class__.requests.append(
            {
                "path": self.path,
                "body": body,
                "authorization": self.headers.get("Authorization"),
            }
        )
        if self.path == "/sessions":
            response = {
                "session_id": "harness-proxied",
                "harness": body.get("harness"),
                "cwd": body.get("cwd"),
            }
            # Mirror harnessd: the structured create response advertises its
            # mode so clients can route to the structured UI.
            if body.get("mode") == "structured":
                response["mode"] = "structured"
        elif self.path == "/sessions/harness-running/terminate":
            response = {"session_id": "harness-running", "status": "terminated"}
        elif self.path == "/sessions/harness-running/turns":
            response = {"turn_id": "turn-xyz"}
        elif self.path == "/sessions/harness-running/permission":
            response = {"ok": True}
        elif self.path == "/sessions/harness-running/interrupt":
            response = {"ok": True}
        else:
            self.send_response(404)
            self.end_headers()
            return
        payload = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args: object) -> None:
        return


def _connect_ws(base_url: str, path: str) -> socket.socket:
    host_port = base_url.removeprefix("http://")
    host, port = host_port.split(":", 1)
    sock = socket.create_connection((host, int(port)), timeout=5)
    client_handshake(sock, host=host_port, path=path)
    sock.settimeout(5)
    return sock


_TEST_TOKEN = "test-token"
_TEST_AUTH = AuthSettings(enabled=True, api_token=_TEST_TOKEN)
_AUTH_HEADERS = {"Authorization": f"Bearer {_TEST_TOKEN}"}


def _authed_get(url: str, headers: dict[str, str] | None = None):
    request = urllib.request.Request(url, headers={**_AUTH_HEADERS, **(headers or {})})
    return urllib.request.urlopen(request, timeout=5)


def _authed_post(url: str, payload: object):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={**_AUTH_HEADERS, "Content-Type": "application/json"},
        method="POST",
    )
    return urllib.request.urlopen(request, timeout=5)


def _authed_delete(url: str):
    request = urllib.request.Request(url, headers=_AUTH_HEADERS, method="DELETE")
    return urllib.request.urlopen(request, timeout=5)


def _observe_insight(collector: MetricsCollector):
    return AdvisoryRepository(collector.duckdb_path).observe(
        FindingCandidate(
            analyzer_id="hooks",
            rule_id="hook.executable_missing",
            target_type="hook",
            target_id="mac-mini:session-start",
            analyzer_class=AnalyzerClass.DETERMINISTIC,
            severity=Severity.HIGH,
            confidence=Confidence.CONFIRMED,
            title="SessionStart hook executable is missing",
            impact="New sessions skip required setup.",
            remediation=("Restore the executable.",),
            evidence=(
                FindingEvidence(
                    source_ref="host:mac-mini/hooks/session-start",
                    observed_at=datetime(2026, 8, 8, 17, tzinfo=timezone.utc),
                    fields={"exists": False},
                    excerpt="missing executable",
                ),
            ),
            content_hash="hash-v1",
        ),
        run_id="analysis-run-1",
    )


def _observe_provider_insight(collector: MetricsCollector):
    with duckdb.connect(str(collector.duckdb_path)) as con:
        con.execute(
            """
            INSERT INTO provider_connections (
              provider, account_label, host_id, enabled, updated_at
            ) VALUES ('openai', 'personal', 'mac-mini', TRUE, ?)
            """,
            [datetime(2026, 8, 8, 17, tzinfo=timezone.utc)],
        )
    return AdvisoryRepository(collector.duckdb_path).observe(
        FindingCandidate(
            analyzer_id="deterministic.connector_freshness",
            rule_id="connector.stale",
            target_type="provider_connector",
            target_id="mac-mini/openai/personal",
            analyzer_class=AnalyzerClass.DETERMINISTIC,
            severity=Severity.HIGH,
            confidence=Confidence.CONFIRMED,
            title="OpenAI connector data is stale",
            impact="Provider capacity may be stale.",
            remediation=("Refresh the connector, then run Check Again.",),
            evidence=(
                FindingEvidence(
                    source_ref="provider_connections:mac-mini/openai/personal",
                    observed_at=datetime(2026, 8, 8, 17, tzinfo=timezone.utc),
                    fields={"status": "stale"},
                ),
            ),
            content_hash="old-hash",
        ),
        run_id="old-run",
    )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _make_collector(tmp_path) -> MetricsCollector:
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    return MetricsCollector(
        duckdb_path=duckdb_path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        ttl_seconds=60,
    )


def collector_with_session(
    tmp_path,
    *,
    preview: str,
    recap: tuple[str, int] | None = None,
    event_type: str = "user_input",
) -> MetricsCollector:
    """Create one fleet session with a prompt and optional recap projection."""
    collector = _make_collector(tmp_path)
    registry = HarnessRegistry(collector.duckdb_path)
    session = registry.create_session(
        host_id="mac-mini",
        harness="codex",
        command="codex",
        status="running",
        cwd="/Volumes/M2 1/drover",
    )
    registry.append_event(
        session_id=session.session_id,
        event_type=event_type,
        content_preview=preview,
    )
    if recap is not None:
        with registry._connect() as con:
            con.execute(
                """INSERT INTO live_session_recaps
                   (session_id, recap_text, source_seq, generator_model, generated_at)
                   VALUES (?, ?, ?, 'test-recap-model', now())""",
                [session.session_id, recap[0], recap[1]],
            )
    return collector


def _swift_content_consent_fixture(name: str) -> dict:
    fixture = (
        Path(__file__).parents[1]
        / "apps/drover/DroverKit/Tests/DroverKitTests/Fixtures"
        / f"content-consent-{name}.json"
    )
    return json.loads(fixture.read_text(encoding="utf-8"))


class _FailingWriter:
    def __init__(self, error: Exception):
        self.error = error

    def write(self, payload: bytes) -> None:
        raise self.error


def _send_handler(*, writer, end_headers=lambda: None):
    class _Handler:
        path = "/harness/sessions/legacy/messages"
        wfile = writer

        @staticmethod
        def send_response(status):
            return None

        @staticmethod
        def send_header(name, value):
            return None

    handler = _Handler()
    handler.end_headers = end_headers
    return handler


@pytest.mark.parametrize("error", [BrokenPipeError(), ConnectionResetError()])
def test_send_treats_expected_final_write_disconnect_as_access_outcome(caplog, error):
    handler = _send_handler(writer=_FailingWriter(error))

    with caplog.at_level(logging.INFO, logger="drover.metrics"):
        _MetricsHandler._send(handler, 200, "application/json", "secret message")

    assert "client disconnected while sending 14 bytes" in caplog.text
    assert "secret message" not in caplog.text


def test_send_does_not_swallow_other_final_write_errors():
    handler = _send_handler(writer=_FailingWriter(OSError("disk failure")))

    with pytest.raises(OSError, match="disk failure"):
        _MetricsHandler._send(handler, 200, "application/json", "{}")


def test_send_does_not_swallow_disconnect_before_final_write():
    handler = _send_handler(
        writer=_FailingWriter(AssertionError("write must not run")),
        end_headers=lambda: (_ for _ in ()).throw(BrokenPipeError("headers")),
    )

    with pytest.raises(BrokenPipeError, match="headers"):
        _MetricsHandler._send(handler, 200, "application/json", "{}")


def _json_request(url: str, *, payload: dict | None = None):
    data = None
    headers = dict(_AUTH_HEADERS)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers)
    with urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def _recv_json(sock: socket.socket) -> dict:
    frame = recv_frame(sock)
    return json.loads(frame.payload.decode("utf-8"))


def _wait_for_output(sock: socket.socket, needle: str) -> str:
    deadline = monotonic() + 5
    collected = ""
    while monotonic() < deadline:
        message = _recv_json(sock)
        if message.get("type") == "output":
            collected += message.get("data", "")
            if needle in collected:
                return collected
    raise AssertionError(f"did not observe {needle!r}; collected={collected!r}")


def test_metrics_collector_renders_quality_summarizer_and_redis(monkeypatch, tmp_path):
    monkeypatch.setattr(metrics, "quality_snapshot", lambda **_: _snapshot())
    collector = MetricsCollector(
        duckdb_path=tmp_path / "missing.duckdb",
        incoming_dir=tmp_path / "incoming",
        summarizer_report={
            "backend_policy": "hybrid",
            "anthropic_ready": True,
            "harness_ready": True,
        },
        job_streams={"summarize": _Stream()},
        ttl_seconds=60,
    )

    text = collector.render_prometheus()

    assert 'drover_quality_score{category="overall"} 0.85' in text
    assert "drover_summary_coverage_percent 100.0" in text
    assert "drover_session_embedding_coverage_percent 100.0" in text
    assert "drover_span_embedding_coverage_percent 99.5" in text
    assert "drover_bundle_ready_percent 64.2" in text
    assert "drover_openclaw_unmatched_spans 5" in text
    assert 'drover_summarizer_policy{policy="hybrid"} 1' in text
    assert 'drover_summarizer_backend_ready{backend="anthropic"} 1' in text
    assert 'drover_summarizer_backend_ready{backend="claude-code"} 1' in text
    assert 'drover_summarizer_backend_allowed{backend="claude-code"} 1' in text
    assert 'drover_redis_job_stream_length{queue="summarize"} 4' in text
    assert 'drover_redis_job_stream_pending{queue="summarize"} 1' in text
    assert (
        'drover_agent_adoption_ready{runtime="openclaw-main",status="active"} 1' in text
    )
    assert 'drover_agent_adoption_observed_events{runtime="openclaw-main"} 12' in text


def test_metrics_sequence_and_bounded_retry_health_hide_session_ids(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(metrics, "quality_snapshot", lambda **_: _snapshot())
    db = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=db)
    with duckdb.connect(str(control_plane_path(db))) as con:
        con.executemany(
            "INSERT INTO harness_events "
            "(event_id, session_id, event_type, payload_json, created_at, seq) "
            "VALUES (?, ?, 'user_input', '{}', now(), ?)",
            [
                ("legacy-1", "private-legacy-session", None),
                ("mixed-1", "private-mixed-session", 1),
                ("mixed-2", "private-mixed-session", None),
            ],
        )
    # summarize_jobs is analytical and stayed in the lakehouse.
    with duckdb.connect(str(db)) as con:
        con.executemany(
            "INSERT INTO summarize_jobs "
            "(session_id, status, attempts, max_attempts, next_run_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    "private-retry-session",
                    "retry_wait",
                    2,
                    5,
                    datetime.now(timezone.utc).replace(tzinfo=None)
                    + timedelta(minutes=5),
                    datetime.now(timezone.utc).replace(tzinfo=None)
                    - timedelta(minutes=2),
                ),
                (
                    "private-dead-session",
                    "dead_lettered",
                    7,
                    7,
                    None,
                    datetime.now() - timedelta(minutes=10),
                ),
                ("private-unbounded-label", "private-status", 1, 99, None, None),
            ],
        )
    collector = MetricsCollector(
        duckdb_path=db,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        ttl_seconds=60,
    )

    text = collector.render_prometheus()

    assert "drover_harness_legacy_unsequenced_events 2" in text
    assert "drover_harness_mixed_sequence_sessions 1" in text
    assert 'drover_summarize_jobs{status="retry_wait"} 1' in text
    assert 'drover_summarize_jobs{status="dead_lettered"} 1' in text
    assert "drover_summarize_max_attempts 99" in text
    oldest = next(
        line
        for line in text.splitlines()
        if line.startswith("drover_summarize_oldest_retry_seconds ")
    )
    oldest_seconds = float(oldest.rsplit(" ", 1)[1])
    assert 119 <= oldest_seconds <= 125
    assert "private-retry-session" not in text
    assert "private-dead-session" not in text
    assert 'status="private-status"' not in text


def test_sequence_health_report_does_not_materialize_event_metadata(
    monkeypatch, tmp_path
):
    db = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=db)
    with duckdb.connect(str(control_plane_path(db))) as con:
        con.execute(
            "INSERT INTO harness_events "
            "(event_id, session_id, event_type, payload_json, created_at, seq) "
            "VALUES ('event-1', 'session-1', 'user_input', '{}', now(), NULL)"
        )
    real = duckdb.connect(str(control_plane_path(db)))

    class AggregateOnlyConnection:
        def execute(self, query, parameters=None):
            normalized = " ".join(str(query).lower().split())
            assert "select event_id, session_id, created_at, seq" not in normalized
            if parameters is None:
                return real.execute(query)
            return real.execute(query, parameters)

        def close(self):
            real.close()

    # `sequence_health_report` reads harness_events through the control plane's
    # connection since #95, so that is the seam to intercept.
    @contextmanager
    def aggregate_only(*args, **kwargs):
        yield AggregateOnlyConnection()

    monkeypatch.setattr(metrics, "control_plane_connection", aggregate_only)

    assert metrics.sequence_health_report(db) == {
        "null_event_count": 1,
        "all_null_sessions": 1,
        "mixed_sessions": 0,
    }


def test_metrics_collector_json_surface(monkeypatch, tmp_path):
    monkeypatch.setattr(metrics, "quality_snapshot", lambda **_: _snapshot())
    collector = MetricsCollector(
        duckdb_path=Path(tmp_path / "missing.duckdb"),
        incoming_dir=tmp_path / "incoming",
        summarizer_report={"backend_policy": "cloud"},
        job_streams={"summarize": _Stream()},
        ttl_seconds=60,
    )

    body = collector.render_json()

    assert '"status": "warn"' in body
    assert '"backend_policy": "cloud"' in body
    assert '"redis_streams"' in body
    assert '"summarize"' in body
    assert '"pending": 1' in body
    assert '"undelivered": 3' in body


def test_metrics_http_server_serves_observatory_ui(monkeypatch, tmp_path):
    monkeypatch.setattr(metrics, "quality_snapshot", lambda **_: _snapshot())
    collector = MetricsCollector(
        duckdb_path=Path(tmp_path / "missing.duckdb"),
        incoming_dir=tmp_path / "incoming",
        summarizer_report={"backend_policy": "cloud"},
        ttl_seconds=60,
    )
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        port = server.server_address[1]
        with _authed_get(f"http://127.0.0.1:{port}/") as res:
            body = res.read().decode("utf-8")
    finally:
        server.shutdown()
        server.server_close()

    assert res.status == 200
    assert "Drover Observatory" in body
    assert "Pipeline Flow" in body
    assert "Embed Span" in body
    assert 'fetch("/observability"' in body


def test_harness_session_html_keeps_terminal_regex_escapes_literal():
    html = load_page("harness_terminal.html")

    assert "\x1b" not in html
    assert "\x07" not in html
    assert "/\\x1b\\]" in html
    assert "[\\x00-\\x08\\x0b-\\x1f\\x7f]" in html
    assert 'data.replace(/\\r?\\n/g, "\\r")' in html
    assert 'data: "\\r"' in html


def test_metrics_collector_harness_json(tmp_path):
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    registry = HarnessRegistry(duckdb_path)
    registry.register_host(
        host_id="nas",
        display_name="NAS",
        kind="linux",
        local_url="http://192.168.1.70:7081",
        capabilities={"harnesses": [{"name": "shell", "enabled": True}]},
    )
    registry.create_session(
        host_id="nas",
        harness="shell",
        command="/bin/sh",
        status="running",
        cwd="/home/Arnab/dev/nexus",
    )
    collector = MetricsCollector(
        duckdb_path=duckdb_path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        ttl_seconds=60,
    )

    body = collector.render_harness_json()

    assert '"host_id": "nas"' in body
    assert '"harness": "shell"' in body
    assert '"local_url": "http://192.168.1.70:7081"' in body
    assert '"cwd_suggestions"' in body
    assert '"path": "/home/Arnab/dev/nexus"' in body
    assert '"source": "recent session"' in body


def test_harness_capabilities_advertise_cockpit_api(tmp_path):
    payload = json.loads(_make_collector(tmp_path).render_harness_json())

    assert payload["cockpit_api_version"] == 1
    assert payload["cockpit_sections"] == [
        "provider_capacity",
        "activity",
        "popular_projects",
        "insights",
    ]


def test_cockpit_endpoints_require_auth_and_reject_unknown_filters(tmp_path):
    collector = _make_collector(tmp_path)
    collector.cockpit_service = CockpitService(
        duckdb_path=collector.duckdb_path,
        provider_usage=None,
    )
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with pytest.raises(HTTPError) as exc:
            urlopen(base + "/analytics", timeout=3)
        assert exc.value.code == 401

        with pytest.raises(HTTPError) as exc:
            _authed_get(base + "/analytics?days=0")
        assert exc.value.code == 400

        with pytest.raises(HTTPError) as exc:
            _authed_get(base + "/analytics?unexpected=value")
        assert exc.value.code == 400

        with pytest.raises(HTTPError) as exc:
            _authed_get(base + "/analytics?limit=1&limit=2")
        assert exc.value.code == 400

        with pytest.raises(HTTPError) as exc:
            _authed_get(base + "/analytics?limit=101")
        assert exc.value.code == 400

        with pytest.raises(HTTPError) as exc:
            _authed_get(base + "/analytics?project_cursor=not-a-valid-cursor")
        assert exc.value.code == 400

        with _authed_get(base + "/cockpit/overview?days=7") as response:
            payload = json.loads(response.read())
        assert payload["activity"]["status"] == "ok"
        assert payload["provider_capacity"]["status"] == "unavailable"
    finally:
        server.shutdown()
        server.server_close()


def test_analytics_endpoint_returns_snapshot_changed_conflict(tmp_path):
    collector = _make_collector(tmp_path)

    class _ChangedCockpit:
        def analytics(self, filters):
            from drover.server.cockpit.analytics import AnalyticsSnapshotChangedError

            raise AnalyticsSnapshotChangedError()

    collector.cockpit_service = _ChangedCockpit()
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with pytest.raises(HTTPError) as exc:
            _authed_get(base + "/analytics?days=7")
        assert exc.value.code == 409
        assert json.loads(exc.value.read()) == {
            "detail": "Activity changed; reload analytics from the first page.",
            "error": "snapshot_changed",
        }
    finally:
        server.shutdown()
        server.server_close()


def test_insights_endpoints_require_auth_and_reject_unknown_filters(tmp_path):
    collector = _make_collector(tmp_path)
    _observe_insight(collector)
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with pytest.raises(HTTPError) as exc:
            urlopen(base + "/insights", timeout=3)
        assert exc.value.code == 401

        with pytest.raises(HTTPError) as exc:
            _authed_get(base + "/insights?unexpected=value")
        assert exc.value.code == 400

        with _authed_get(base + "/insights?severity=high&limit=1") as response:
            payload = json.loads(response.read())
        assert payload["findings"][0]["severity"] == "high"
    finally:
        server.shutdown()
        server.server_close()


def test_content_analysis_privacy_routes_require_auth_and_return_bounded_status(
    tmp_path,
):
    collector = _make_collector(tmp_path)
    config_path = tmp_path / "config.toml"
    config_path.write_text("""[advisory_content]
enabled = false
backend_policy = "local"
external_consent = false
targets = []
allowed_roots = []
max_file_bytes = 131072
max_bundle_bytes = 524288
excerpt_max_chars = 320
""")
    collector.advisory_service = InsightsService(
        collector.duckdb_path, config_path=config_path
    )
    _observe_insight(collector)
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        for method, path, payload in (
            ("GET", "/insights/content-analysis", None),
            ("POST", "/insights/content-analysis/consent", {"backend": "local"}),
            ("POST", "/insights/content-analysis/revoke", {}),
            ("DELETE", "/insights/content-excerpts", None),
        ):
            request = urllib.request.Request(
                base + path,
                data=(json.dumps(payload).encode() if payload is not None else None),
                headers=(
                    {"Content-Type": "application/json"} if payload is not None else {}
                ),
                method=method,
            )
            with pytest.raises(HTTPError) as exc:
                urlopen(request, timeout=3)
            assert exc.value.code == 401

        with _authed_get(base + "/insights/content-analysis") as response:
            status = json.loads(response.read())
        assert status == _swift_content_consent_fixture("complete")
        assert "content" not in json.dumps(status).lower().replace(
            "content_analysis", ""
        )

        with _authed_post(
            base + "/insights/content-analysis/consent", {"backend": "local"}
        ) as response:
            consent = json.loads(response.read())
        assert consent["enabled"] is True
        assert consent["external_disclosure_accepted"] is False

        with pytest.raises(HTTPError) as exc:
            _authed_post(
                base + "/insights/content-analysis/consent",
                {"backend": "cloud", "external_disclosure_accepted": False},
            )
        assert exc.value.code == 400

        with _authed_post(base + "/insights/content-analysis/revoke", {}) as response:
            revoked = json.loads(response.read())
        assert revoked["enabled"] is False

        with _authed_delete(base + "/insights/content-excerpts") as response:
            purged = json.loads(response.read())
        assert purged == {"purged_excerpt_count": 1}
    finally:
        server.shutdown()
        server.server_close()


def test_content_status_empty_registry_matches_shared_swift_fixture(tmp_path) -> None:
    collector = _make_collector(tmp_path)
    collector.advisory_service = InsightsService(
        collector.duckdb_path, config_path=tmp_path / "config.toml"
    )

    status, body = collector.render_content_analysis_status_json()

    assert status == 200
    assert json.loads(body) == _swift_content_consent_fixture("complete")


def test_content_status_offline_registry_matches_shared_swift_fixture(tmp_path) -> None:
    collector = _make_collector(tmp_path)
    HarnessRegistry(collector.duckdb_path).register_host(
        host_id="offline-laptop",
        display_name="Offline Laptop",
        kind="macos",
        connection_kind="relay",
        status="offline",
    )
    collector.advisory_service = InsightsService(
        collector.duckdb_path, config_path=tmp_path / "config.toml"
    )
    collector.consent_content_analysis(
        {"backend": "cloud", "external_disclosure_accepted": True}
    )

    status, body = collector.render_content_analysis_status_json()

    assert status == 207
    assert json.loads(body) == _swift_content_consent_fixture("partial")


def test_content_status_failed_registry_matches_shared_swift_fixture(tmp_path) -> None:
    collector = _make_collector(tmp_path)
    HarnessRegistry(collector.duckdb_path).register_host(
        host_id="workstation",
        display_name="Workstation",
        kind="macos",
        connection_kind="relay",
    )
    collector.advisory_service = InsightsService(
        collector.duckdb_path, config_path=tmp_path / "config.toml"
    )

    class _StatusNackRelay:
        def is_live(self, host_id):
            return True

        def request(self, host_id, method, path, body, timeout_s=15):
            return 409, '{"error":"epoch conflict"}'

    collector.relay_manager = _StatusNackRelay()

    status, body = collector.render_content_analysis_status_json()

    assert status == 503
    assert json.loads(body) == _swift_content_consent_fixture("failed")


def test_content_status_repair_failure_matches_shared_swift_fixture(
    tmp_path, monkeypatch
) -> None:
    """Catches durable repair failure becoming a generic or successful GET."""

    collector = _make_collector(tmp_path)
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """[advisory_content]
enabled = true
backend_policy = "cloud"
external_consent = true
targets = []
allowed_roots = []
max_file_bytes = 131072
max_bundle_bytes = 524288
excerpt_max_chars = 320
""",
        encoding="utf-8",
    )
    collector.advisory_service = InsightsService(
        collector.duckdb_path, config_path=config_path
    )
    monkeypatch.setattr(
        collector.advisory_service._content_consent,
        "_persist",
        lambda state: (_ for _ in ()).throw(OSError("disk unavailable")),
    )

    status, body = collector.render_content_analysis_status_json()

    assert status == 503
    assert json.loads(body) == _swift_content_consent_fixture("repair-failed")


def test_central_consent_and_revoke_reconcile_an_already_running_direct_daemon(
    tmp_path,
):
    """Catches consent APIs succeeding while a live host keeps stale state."""

    target = tmp_path / "AGENTS.md"
    target.write_text("Use the deployment skill.\n", encoding="utf-8")
    host_db = tmp_path / "host.duckdb"
    bootstrap(parquet_dir=tmp_path / "host-parquet", duckdb_path=host_db)
    host_state = HarnessDaemonState(
        host_id="mac-mini",
        display_name="Mac Mini",
        kind="macos",
        registry=HarnessRegistry(host_db),
        pty=PtySessionManager(),
        presets=DEFAULT_PRESETS,
        api_token="secret",
        advisory_content=AdvisoryContentConfig(
            enabled=True,
            backend_policy="local",
            external_consent=False,
            targets=(str(target),),
            allowed_roots=(tmp_path,),
            max_file_bytes=1024,
            max_bundle_bytes=2048,
            excerpt_max_chars=320,
        ),
        content_consent=DurableContentConsent(tmp_path / "host-consent.json"),
    )
    host_server = create_harness_server(
        listen_host="127.0.0.1", listen_port=0, state=host_state
    )
    host_thread = threading.Thread(target=host_server.serve_forever, daemon=True)
    host_thread.start()

    collector = _make_collector(tmp_path / "central")
    HarnessRegistry(collector.duckdb_path).register_host(
        host_id="mac-mini",
        display_name="Mac Mini",
        kind="macos",
        local_url=f"http://127.0.0.1:{host_server.server_port}",
    )
    config_path = tmp_path / "central-config.toml"
    collector.advisory_service = InsightsService(
        collector.duckdb_path, config_path=config_path
    )
    collector.api_token = "secret"

    try:
        status, body = collector.consent_content_analysis({"backend": "local"})
        assert status == 200
        consent = json.loads(body)
        assert consent["propagation"] == "complete"
        assert host_state.content_consent.snapshot() == {
            "enabled": True,
            "epoch": consent["consent_epoch"],
        }

        status, body = collector.render_content_analysis_status_json()
        reconciled = json.loads(body)
        assert status == 200
        assert reconciled["consent_epoch"] == consent["consent_epoch"]
        assert reconciled["hosts"] == [{"host_id": "mac-mini", "state": "acknowledged"}]

        bundle = collector.fetch_advisory_content_bundle("mac-mini", ["AGENTS.md"])
        assert bundle["targets"][0]["target_id"] == "AGENTS.md"

        status, body = collector.revoke_content_analysis({})
        assert status == 200
        revoked = json.loads(body)
        assert revoked["propagation"] == "complete"
        assert host_state.content_consent.snapshot() == {
            "enabled": False,
            "epoch": revoked["consent_epoch"],
        }
        with pytest.raises(RuntimeError, match="disabled"):
            collector.fetch_advisory_content_bundle("mac-mini", ["AGENTS.md"])
    finally:
        host_state.pty.close_all()
        host_server.shutdown()
        host_server.server_close()


def test_insight_detail_and_lifecycle_actions_are_validated(tmp_path):
    collector = _make_collector(tmp_path)
    finding = _observe_insight(collector)
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with _authed_get(base + f"/insights/{finding.finding_id}") as response:
            detail = json.loads(response.read())
        assert detail["finding"]["finding_id"] == finding.finding_id
        assert detail["actions"]["check_again"]["available"] is False

        with pytest.raises(HTTPError) as exc:
            _authed_post(base + f"/insights/{finding.finding_id}/check", {})
        assert exc.value.code == 409

        with pytest.raises(HTTPError) as exc:
            _authed_post(base + f"/insights/{finding.finding_id}/dismiss", {})
        assert exc.value.code == 400

        with _authed_post(
            base + f"/insights/{finding.finding_id}/acknowledge", {}
        ) as response:
            acknowledged = json.loads(response.read())
        assert acknowledged["finding"]["state"] == "acknowledged"

        with pytest.raises(HTTPError) as exc:
            _authed_post(base + f"/insights/{finding.finding_id}/acknowledge", {})
        assert exc.value.code == 409

        with _authed_post(
            base + f"/insights/{finding.finding_id}/dismiss",
            {"reason": "accepted tradeoff"},
        ) as response:
            dismissed = json.loads(response.read())
        assert dismissed["finding"]["state"] == "dismissed"
    finally:
        server.shutdown()
        server.server_close()


def test_check_again_enqueues_without_configuration_mutation(tmp_path):
    collector = _make_collector(tmp_path)
    finding = _observe_provider_insight(collector)
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with _authed_post(
            base + f"/insights/{finding.finding_id}/check", {}
        ) as response:
            assert response.status == 202
            payload = json.loads(response.read())
        assert payload["status"] == "queued"

        con = duckdb.connect(str(collector.duckdb_path), read_only=True)
        try:
            jobs = con.execute(
                "SELECT job_kind, subject_key FROM pipeline_jobs"
            ).fetchall()
        finally:
            con.close()
        assert jobs == [
            (
                "analyze_advisory_target",
                "deterministic.connector_freshness:mac-mini",
            )
        ]
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/insights/not-a-finding/check", {}),
        ("/insights/00000000000000000000000000000000/dismiss", []),
        ("/insights/00000000000000000000000000000000/acknowledge", {"extra": 1}),
    ],
)
def test_insight_routes_reject_invalid_ids_and_json(tmp_path, path, payload):
    collector = _make_collector(tmp_path)
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with pytest.raises(HTTPError) as exc:
            _authed_post(base + path, payload)
        assert exc.value.code == 400
    finally:
        server.shutdown()
        server.server_close()


def test_insight_api_failure_is_section_local(tmp_path):
    class _FailedInsights:
        def list_insights(self, filters):
            raise RuntimeError("analyzer database unavailable")

    collector = _make_collector(tmp_path)
    collector.advisory_service = _FailedInsights()
    collector.cockpit_service = CockpitService(
        duckdb_path=collector.duckdb_path,
        provider_usage=None,
    )
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with pytest.raises(HTTPError) as exc:
            _authed_get(base + "/insights")
        assert exc.value.code == 503
        assert "analyzer database unavailable" not in exc.value.read().decode()

        with _authed_get(base + "/cockpit/overview") as response:
            assert json.loads(response.read())["activity"]["status"] == "ok"
    finally:
        server.shutdown()
        server.server_close()


def test_provider_refresh_loop_only_refreshes_online_hosts_once_per_interval():
    refreshed = []

    class _Registry:
        def list_hosts(self, *, status=None):
            assert status is None
            return [type("Host", (), {"host_id": "mac-mini"})()]

    class _ProviderUsage:
        def refresh_host(self, host):
            refreshed.append(host.host_id)

    stop = threading.Event()
    loop = ProviderRefreshLoop(
        provider_usage=_ProviderUsage(),
        registry=_Registry(),
        shutdown_event=stop,
        interval_seconds=300,
    )

    loop.run_once()
    loop.run_once()

    assert refreshed == ["mac-mini"]


def test_harness_snapshot_serializes_session_dates_with_timezone(tmp_path):
    collector = _make_collector(tmp_path)
    registry = HarnessRegistry(collector.duckdb_path)
    session = registry.create_session(
        host_id="mac-mini",
        harness="codex",
        command="codex",
        status="running",
        cwd="/Volumes/M2 1/drover",
        started_at=datetime(2026, 8, 5, 10, 40, 37, tzinfo=timezone.utc),
    )
    registry.update_session_activity(
        session.session_id,
        awaiting="input",
        last_activity=datetime(2026, 8, 5, 10, 55, 34, tzinfo=timezone.utc),
    )

    payload = json.loads(collector.render_harness_json())

    rendered = payload["sessions"][0]
    started_at = datetime.fromisoformat(rendered["started_at"])
    last_activity = datetime.fromisoformat(rendered["last_activity"])
    assert started_at.tzinfo is not None
    assert last_activity.tzinfo is not None
    assert started_at.astimezone(timezone.utc) == datetime(
        2026, 8, 5, 10, 40, 37, tzinfo=timezone.utc
    )
    assert last_activity.astimezone(timezone.utc) == datetime(
        2026, 8, 5, 10, 55, 34, tzinfo=timezone.utc
    )


def test_wire_datetime_adds_offset_to_naive_server_strings():
    rendered = metrics._wire_datetime("2026-08-05 14:01:49.805981")

    assert rendered is not None
    assert datetime.fromisoformat(rendered).tzinfo is not None


def test_wire_datetime_normalizes_aware_values_to_utc():
    pacific = timezone(timedelta(hours=-7))

    rendered = metrics._wire_datetime(datetime(2026, 8, 5, 14, 1, 49, tzinfo=pacific))

    assert rendered == "2026-08-05T21:01:49+00:00"


def test_harness_snapshot_includes_latest_user_or_assistant_preview(tmp_path):
    collector = _make_collector(tmp_path)
    registry = HarnessRegistry(collector.duckdb_path)
    session = registry.create_session(
        host_id="mac-mini",
        harness="codex",
        command="codex",
        status="running",
        cwd="/Volumes/M2 1/drover",
    )
    registry.append_event(
        session_id=session.session_id,
        event_type="status",
        content_preview="turn started",
    )
    registry.append_event(
        session_id=session.session_id,
        event_type="user_input",
        content_preview="Refactor session screen cards",
    )
    registry.append_event(
        session_id=session.session_id,
        event_type="tool_action",
        content_preview="git status --short",
    )

    payload = json.loads(collector.render_harness_json())

    assert payload["sessions"][0]["preview"] == "Refactor session screen cards"


def test_harness_snapshot_includes_live_recap_and_preview_fallback(tmp_path):
    collector = collector_with_session(
        tmp_path,
        preview="Improve the chat list",
        recap=("Improving chat titles; wiring recap refresh.", 12),
    )

    payload = collector.harness_snapshot()

    session = payload["sessions"][0]
    assert session["preview"] == "Improve the chat list"
    assert session["recap"] == "Improving chat titles; wiring recap refresh."
    assert session["recap_source_seq"] == 12


def test_harness_snapshot_missing_recap_emits_null_fields(tmp_path):
    collector = collector_with_session(tmp_path, preview="Improve the chat list")

    session = collector.harness_snapshot()["sessions"][0]

    assert session["preview"] == "Improve the chat list"
    assert session["recap"] is None
    assert session["recap_source_seq"] is None


def test_harness_snapshot_recap_preserves_terminal_preview(tmp_path):
    collector = collector_with_session(
        tmp_path,
        preview="git status --short",
        recap=("Checking the working tree before the snapshot change.", 9),
        event_type="terminal.input",
    )

    session = collector.harness_snapshot()["sessions"][0]

    assert session["preview"] == "git status --short"
    assert session["recap"] == "Checking the working tree before the snapshot change."
    assert session["recap_source_seq"] == 9


def test_metrics_collector_marks_stale_harness_hosts(tmp_path):
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    registry = HarnessRegistry(duckdb_path)
    registry.register_host(
        host_id="nas",
        display_name="NAS",
        kind="linux",
        local_url="http://192.168.1.70:7081",
        capabilities={"harnesses": [{"name": "shell", "enabled": True}]},
    )
    stale_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    with registry._connect() as con:
        con.execute(
            "UPDATE harness_hosts SET last_seen_at = ?, updated_at = ? WHERE host_id = ?",
            [stale_at, stale_at, "nas"],
        )
    collector = MetricsCollector(
        duckdb_path=duckdb_path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        ttl_seconds=60,
    )

    payload = json.loads(collector.render_harness_json())

    assert payload["hosts"][0]["host_id"] == "nas"
    assert payload["hosts"][0]["status"] == "stale"


def test_metrics_collector_keeps_fresh_naive_harness_hosts_online(tmp_path):
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    registry = HarnessRegistry(duckdb_path)
    registry.register_host(
        host_id="nas",
        display_name="NAS",
        kind="linux",
        local_url="http://192.168.1.70:7081",
        capabilities={"harnesses": [{"name": "shell", "enabled": True}]},
    )
    fresh_at = datetime.now()
    with registry._connect() as con:
        con.execute(
            "UPDATE harness_hosts SET last_seen_at = ?, updated_at = ? WHERE host_id = ?",
            [fresh_at, fresh_at, "nas"],
        )
    collector = MetricsCollector(
        duckdb_path=duckdb_path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        ttl_seconds=60,
    )

    payload = json.loads(collector.render_harness_json())

    assert payload["hosts"][0]["host_id"] == "nas"
    assert payload["hosts"][0]["status"] == "online"
    assert "stale_after_seconds" not in payload["hosts"][0]


def test_metrics_collector_harness_cwd_suggestions_are_recent_then_favorites(tmp_path):
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    registry = HarnessRegistry(duckdb_path)
    registry.register_host(
        host_id="nas",
        display_name="NAS",
        kind="linux",
        local_url="http://192.168.1.70:7081",
        capabilities={"harnesses": [{"name": "shell", "enabled": True}]},
    )
    registry.register_host(
        host_id="mac-mini",
        display_name="Mac Mini",
        kind="macos",
        local_url="http://192.168.1.149:7081",
        capabilities={"harnesses": [{"name": "shell", "enabled": True}]},
    )
    registry.create_session(
        host_id="nas",
        harness="shell",
        command="/bin/sh",
        status="running",
        cwd="/home/Arnab/projects/demo",
    )
    registry.create_session(
        host_id="mac-mini",
        harness="shell",
        command="/bin/zsh",
        status="ended",
        cwd="/Users/arnabmac/jenny/nexus",
    )
    collector = MetricsCollector(
        duckdb_path=duckdb_path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        ttl_seconds=60,
        favorite_cwds=("/home/Arnab/clawd", "/home/Arnab/dev"),
    )

    payload = json.loads(collector.render_harness_json())

    assert payload["cwd_suggestions"][0] == {
        "host_id": "mac-mini",
        "path": "/Users/arnabmac/jenny/nexus",
        "source": "recent session",
    }
    assert payload["cwd_suggestions"][1] == {
        "host_id": "nas",
        "path": "/home/Arnab/projects/demo",
        "source": "recent session",
    }
    assert {"path": "/home/Arnab/clawd", "source": "favorite"} in payload[
        "cwd_suggestions"
    ]
    assert {"path": "/home/Arnab/dev", "source": "favorite"} in payload[
        "cwd_suggestions"
    ]


def test_metrics_collector_cwd_suggestions_no_favorites_by_default(tmp_path):
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    collector = MetricsCollector(
        duckdb_path=duckdb_path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        ttl_seconds=60,
    )

    payload = json.loads(collector.render_harness_json())

    assert payload["cwd_suggestions"] == []


def test_metrics_collector_harness_session_json(tmp_path):
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    registry = HarnessRegistry(duckdb_path)
    registry.register_host(
        host_id="nas",
        display_name="NAS",
        kind="linux",
        tailscale_url="http://100.64.0.10:7081",
        capabilities={"harnesses": [{"name": "shell", "enabled": True}]},
    )
    session = registry.create_session(
        host_id="nas",
        harness="shell",
        command="/bin/sh",
        status="running",
    )
    registry.append_event(
        session_id=session.session_id,
        event_type="terminal.output",
        harness="shell",
        payload={"text": "hello from transcript\n"},
    )
    collector = MetricsCollector(
        duckdb_path=duckdb_path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        ttl_seconds=60,
    )

    status, body = collector.render_harness_session_json(session.session_id)

    assert status == 200
    assert '"session_id": "' in body
    assert '"tailscale_url": "http://100.64.0.10:7081"' in body
    assert "hello from transcript" in body
    payload = json.loads(body)
    assert payload["events"][0]["normalized_type"] == "assistant_output"
    assert payload["events"][0]["normalized_source"] == "inferred_terminal"


def test_metrics_http_server_serves_harness_ui_and_api(tmp_path):
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    registry = HarnessRegistry(duckdb_path)
    registry.register_host(
        host_id="mac-mini",
        display_name="Mac Mini",
        kind="macos",
        local_url="http://192.168.1.149:7081",
        capabilities={"harnesses": [{"name": "shell", "enabled": True}]},
    )
    session = registry.create_session(
        host_id="mac-mini",
        harness="shell",
        command="/bin/sh",
        status="running",
        cwd="/Users/arnabmac/jenny/nexus",
    )
    collector = MetricsCollector(
        duckdb_path=duckdb_path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        ttl_seconds=60,
    )
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        port = server.server_address[1]
        with _authed_get(f"http://127.0.0.1:{port}/ui/harness") as res:
            page = res.read().decode("utf-8")
        with _authed_get(f"http://127.0.0.1:{port}/harness/hosts") as res:
            hosts = res.read().decode("utf-8")
        with _authed_get(
            f"http://127.0.0.1:{port}/ui/harness/sessions/{session.session_id}",
        ) as res:
            terminal_page = res.read().decode("utf-8")
        with _authed_get(
            f"http://127.0.0.1:{port}/harness/sessions/{session.session_id}",
        ) as res:
            session_json = res.read().decode("utf-8")
        missing_session_error = None
        try:
            _authed_get(f"http://127.0.0.1:{port}/ui/harness/sessions/")
        except HTTPError as exc:
            missing_session_error = exc
            missing_session_page = exc.read().decode("utf-8")
    finally:
        server.shutdown()
        server.server_close()

    assert "Drover Harness Console" in page
    assert 'fetch("/harness"' in page
    assert "Start working" in page
    assert "Pick a workspace to start a session" in page
    assert 'id="workspaces"' in page
    assert "Advanced &mdash; choose host, harness, and path manually" in page
    assert 'id="launch-host"' in page
    assert 'id="cwd-suggestions"' in page
    assert 'id="advanced-status"' in page
    assert "focusNextLaunchField" in page
    assert "HARNESS_PREFERENCE" in page
    assert "renderWorkspaces" in page
    assert "startWorkspaceByIndex" in page
    assert "sessionState" in page
    assert "Waiting to start" in page
    assert "Stale - launch incomplete" in page
    assert "Stale - never started" in page
    assert "resumable" in page
    assert 'Resume" : "Continue"' in page
    assert "fetch(`/harness/hosts/${encodeURIComponent(host.host_id)}/sessions`" in page
    assert (
        "fetch(`/harness/sessions/${encodeURIComponent(sessionId)}/terminate`" in page
    )
    assert "terminateSession(button.dataset.kill" in page
    assert "/terminate" in page
    assert "/ui/harness/sessions/" in page
    assert '"host_id": "mac-mini"' in hosts
    assert '"sessions": []' in hosts
    assert "Harness Session" in terminal_page
    assert 'id="show-events"' in terminal_page
    assert 'id="show-terminal"' in terminal_page
    assert 'id="events"' in terminal_page
    assert "Inferred from terminal text" in terminal_page
    assert "renderEvents" in terminal_page
    assert "groupDisplayEvents" in terminal_page
    assert "renderFormattedBody" in terminal_page
    assert "placeholderOnly" in terminal_page
    assert "terminalDerivedNoise" in terminal_page
    assert "assistant_structured" in terminal_page
    assert "Native transcript" in terminal_page
    assert "/native-transcript" in terminal_page
    assert "Native transcript not available yet." in terminal_page
    assert "Summary hides repaint/spinner noise" in terminal_page
    assert "Handoff context" in terminal_page
    assert "sanitizeTerminal" in terminal_page
    assert "Show recovered terminal transcript" in terminal_page
    assert "Resume mode" in terminal_page
    assert "Drover handoff" in terminal_page
    assert "ensureTerminal" in terminal_page
    assert "Raw terminal UI did not load." in terminal_page
    assert (
        "Session summary, events, and continue controls are still available."
        in terminal_page
    )
    assert "terminalSize()" in terminal_page
    assert "new WebSocket(attachUrl)" in terminal_page
    assert 'id="terminate"' in terminal_page
    assert "[process terminated]" in terminal_page
    assert "direct host attach fallback" in terminal_page
    assert 'class="panel shortcut-grid"' in terminal_page
    assert 'data-shortcut="ctrl-d"' in terminal_page
    assert '"ctrl-c": "\\u0003"' in terminal_page
    assert 'id="paste"' in terminal_page
    assert "function submitSuffix()" in terminal_page
    assert 'sessionData?.session?.harness === "codex" ? "\\n" : "\\r"' in terminal_page
    assert "data.endsWith" in terminal_page
    assert "data += suffix" in terminal_page
    assert 'id="continue-session"' in terminal_page
    assert 'id="continue-native"' in terminal_page
    assert "/native-sessions" in terminal_page
    assert "/continue" in terminal_page
    assert "Continue in selected CLI" in terminal_page
    assert (
        "`${protocol}//${location.host}/harness/sessions/${encodeURIComponent(id)}/terminal`"
        in terminal_page
    )
    assert '"session_id": "' in session_json
    assert '"local_url": "http://192.168.1.149:7081"' in session_json
    assert missing_session_error is not None
    assert missing_session_error.code == 404
    assert "Missing harness session id." in missing_session_page


def test_metrics_http_server_registers_remote_harness_host(tmp_path):
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    collector = MetricsCollector(
        duckdb_path=duckdb_path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        ttl_seconds=60,
        # Without this the collector falls back to the real ~/.drover config,
        # and the asserted consent epoch below is read off whatever the live
        # server on this machine has advanced it to. That passes on a clean
        # CI box and fails forever on any host that actually runs Drover.
        config_path=tmp_path / "config.toml",
    )
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        port = server.server_address[1]
        request = Request(
            f"http://127.0.0.1:{port}/harness/hosts",
            data=json.dumps(
                {
                    "host_id": "nas",
                    "display_name": "NAS",
                    "kind": "linux",
                    "local_url": "http://192.168.1.70:7081",
                    "capabilities": {"harnesses": [{"name": "shell", "enabled": True}]},
                }
            ).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", **_AUTH_HEADERS},
        )
        with urlopen(request, timeout=3) as res:
            payload = json.loads(res.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()

    host = HarnessRegistry(duckdb_path).get_host("nas")
    assert payload["host"]["host_id"] == "nas"
    assert payload["content_consent"] == {"enabled": False, "epoch": 0}
    assert host is not None
    assert host.local_url == "http://192.168.1.70:7081"
    assert host.capabilities["harnesses"][0]["enabled"] is True


def test_metrics_http_server_proxies_harness_launch_and_terminate(tmp_path):
    _FakeHarnessHandler.requests = []
    harness_server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeHarnessHandler)
    harness_thread = metrics.threading.Thread(
        target=harness_server.serve_forever, daemon=True
    )
    harness_thread.start()
    harness_port = harness_server.server_address[1]

    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    registry = HarnessRegistry(duckdb_path)
    registry.register_host(
        host_id="mac-mini",
        display_name="Mac Mini",
        kind="macos",
        local_url=f"http://127.0.0.1:{harness_port}",
        capabilities={"harnesses": [{"name": "shell", "enabled": True}]},
    )
    registry.create_session(
        session_id="harness-running",
        host_id="mac-mini",
        harness="shell",
        command="/bin/sh",
        status="running",
    )
    collector = MetricsCollector(
        duckdb_path=duckdb_path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        ttl_seconds=60,
    )
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        port = server.server_address[1]
        launch = Request(
            f"http://127.0.0.1:{port}/harness/hosts/mac-mini/sessions",
            data=json.dumps({"harness": "shell", "cwd": "/tmp"}).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", **_AUTH_HEADERS},
        )
        with urlopen(launch, timeout=3) as res:
            launch_payload = json.loads(res.read().decode("utf-8"))
        terminate = Request(
            f"http://127.0.0.1:{port}/harness/sessions/harness-running/terminate",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json", **_AUTH_HEADERS},
        )
        with urlopen(terminate, timeout=3) as res:
            terminate_payload = json.loads(res.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()
        harness_server.shutdown()
        harness_server.server_close()

    assert launch_payload == {
        "session_id": "harness-proxied",
        "harness": "shell",
        "cwd": "/tmp",
    }
    assert terminate_payload == {
        "session_id": "harness-running",
        "status": "terminated",
    }
    registry_after_proxy = HarnessRegistry(duckdb_path)
    created = registry_after_proxy.get_session("harness-proxied")
    terminated = registry_after_proxy.get_session("harness-running")
    assert created is not None
    assert created.host_id == "mac-mini"
    assert created.harness == "shell"
    assert created.cwd == "/tmp"
    assert terminated is not None
    assert terminated.status == "terminated"
    assert terminated.ended_at is not None


def test_terminate_tombstones_session_missing_on_daemon(tmp_path):
    # Daemon restarted: the registry row is still "running" but harnessd
    # answers 404 for the session id. Central terminate must tombstone the
    # row and report success instead of proxying the 404 to the client.
    _FakeHarnessHandler.requests = []
    harness_server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeHarnessHandler)
    harness_thread = metrics.threading.Thread(
        target=harness_server.serve_forever, daemon=True
    )
    harness_thread.start()
    harness_port = harness_server.server_address[1]

    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    registry = HarnessRegistry(duckdb_path)
    registry.register_host(
        host_id="mac-mini",
        display_name="Mac Mini",
        kind="macos",
        local_url=f"http://127.0.0.1:{harness_port}",
        capabilities={"harnesses": [{"name": "shell", "enabled": True}]},
    )
    registry.create_session(
        session_id="harness-stale",
        host_id="mac-mini",
        harness="shell",
        command="/bin/sh",
        status="running",
    )
    collector = MetricsCollector(
        duckdb_path=duckdb_path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        ttl_seconds=60,
    )
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        port = server.server_address[1]
        terminate = Request(
            f"http://127.0.0.1:{port}/harness/sessions/harness-stale/terminate",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json", **_AUTH_HEADERS},
        )
        with urlopen(terminate, timeout=3) as res:
            status = res.status
            payload = json.loads(res.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()
        harness_server.shutdown()
        harness_server.server_close()

    assert status == 200
    assert payload["session_id"] == "harness-stale"
    assert payload["status"] == "terminated"
    assert payload["stale"] is True
    session = HarnessRegistry(duckdb_path).get_session("harness-stale")
    assert session is not None
    assert session.status == "terminated"
    assert session.ended_at is not None


def test_terminate_tombstones_session_on_unreachable_host(tmp_path):
    # Host offline: proxying the terminate fails outright (connection
    # refused). Central terminate must still tombstone the row and report
    # success instead of surfacing a 502.
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()

    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    registry = HarnessRegistry(duckdb_path)
    registry.register_host(
        host_id="nas",
        display_name="NAS",
        kind="linux",
        local_url=f"http://127.0.0.1:{dead_port}",
        capabilities={"harnesses": [{"name": "shell", "enabled": True}]},
    )
    registry.create_session(
        session_id="harness-unreachable",
        host_id="nas",
        harness="shell",
        command="/bin/sh",
        status="running",
    )
    collector = MetricsCollector(
        duckdb_path=duckdb_path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        ttl_seconds=60,
    )
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        port = server.server_address[1]
        terminate = Request(
            f"http://127.0.0.1:{port}/harness/sessions/harness-unreachable/terminate",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json", **_AUTH_HEADERS},
        )
        with urlopen(terminate, timeout=5) as res:
            status = res.status
            payload = json.loads(res.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()

    assert status == 200
    assert payload["session_id"] == "harness-unreachable"
    assert payload["status"] == "terminated"
    assert payload["stale"] is True
    session = HarnessRegistry(duckdb_path).get_session("harness-unreachable")
    assert session is not None
    assert session.status == "terminated"
    assert session.ended_at is not None


@pytest.fixture
def collector_with_hosts(tmp_path) -> MetricsCollector:
    """Three harness hosts covering ``_harness_request``'s routing paths.

    - "laptop": relay-connected, no URLs at all -- only reachable via a
      live ``RelayManager``.
    - "mini": a direct URL pointing at a dead port on 127.0.0.1 -- proves
      the direct-dial fallback actually ran (connection refused -> 502).
    - "island": no URLs and no relay -- proves the "no reachable endpoint"
      502 short-circuit.
    """
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    registry = HarnessRegistry(duckdb_path)
    registry.register_host(
        host_id="laptop",
        display_name="Laptop",
        kind="macos",
        connection_kind="relay",
    )
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()
    registry.register_host(
        host_id="mini",
        display_name="Mini",
        kind="macos",
        local_url=f"http://127.0.0.1:{dead_port}",
    )
    registry.register_host(
        host_id="island",
        display_name="Island",
        kind="linux",
    )
    return MetricsCollector(
        duckdb_path=duckdb_path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        ttl_seconds=60,
    )


class _FakeRelay:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.timeouts: list[float] = []

    def is_live(self, host_id: str) -> bool:
        return host_id == "laptop"

    def request(self, host_id, method, path, body, timeout_s=15):
        self.calls.append((host_id, method, path, body))
        self.timeouts.append(timeout_s)
        return 200, '{"ok": true}\n'


def _enable_collector_content(collector: MetricsCollector) -> None:
    service = InsightsService(
        collector.duckdb_path,
        config_path=collector.duckdb_path.parent / "test-content-config.toml",
    )
    service.consent_content_analysis(
        backend="local", external_disclosure_accepted=False
    )
    collector.advisory_service = service


def test_content_consent_and_fetch_use_relay_with_exact_epoch_ack(tmp_path) -> None:
    """Catches relay hosts bypassing central consent reconciliation."""

    collector = _make_collector(tmp_path)
    HarnessRegistry(collector.duckdb_path).register_host(
        host_id="laptop",
        display_name="Laptop",
        kind="macos",
        connection_kind="relay",
    )
    collector.advisory_service = InsightsService(
        collector.duckdb_path, config_path=tmp_path / "config.toml"
    )
    redacted_content = "bounded prompt"
    content_hash = hashlib.sha256(redacted_content.encode()).hexdigest()
    bundle_hash = hashlib.sha256(
        json.dumps([["global-agents", content_hash]], separators=(",", ":")).encode()
    ).hexdigest()

    class _ConsentRelay(_FakeRelay):
        def request(
            self,
            host_id,
            method,
            path,
            body,
            timeout_s=15,
            max_response_bytes=None,
        ):
            self.calls.append((host_id, method, path, body))
            if path == "/advisory/content-consent":
                return 200, json.dumps(body)
            return 200, json.dumps(
                {
                    "bundle_hash": bundle_hash,
                    "created_at": "2026-08-09T12:00:00+00:00",
                    "targets": [
                        {
                            "target_id": "global-agents",
                            "content_hash": content_hash,
                            "redacted_content": redacted_content,
                        }
                    ],
                }
            )

    relay = _ConsentRelay()
    collector.relay_manager = relay

    status, body = collector.consent_content_analysis({"backend": "local"})
    consent = json.loads(body)
    assert status == 200
    assert consent["hosts"] == [{"host_id": "laptop", "state": "acknowledged"}]

    collector.fetch_advisory_content_bundle("laptop", ["global-agents"])
    assert [call[2] for call in relay.calls] == [
        "/advisory/content-consent",
        "/advisory/content-consent",
        "/advisory/content-bundle",
    ]


def test_content_consent_reports_partial_and_revoke_fails_on_reachable_nack(
    tmp_path,
) -> None:
    """Catches mutation endpoints claiming success after a reachable host NACK."""

    collector = _make_collector(tmp_path)
    HarnessRegistry(collector.duckdb_path).register_host(
        host_id="laptop",
        display_name="Laptop",
        kind="macos",
        connection_kind="relay",
    )
    collector.advisory_service = InsightsService(
        collector.duckdb_path, config_path=tmp_path / "config.toml"
    )

    class _NackRelay(_FakeRelay):
        def request(self, host_id, method, path, body, timeout_s=15):
            return 409, '{"error":"epoch conflict"}'

    collector.relay_manager = _NackRelay()

    enabled_status, enabled_body = collector.consent_content_analysis(
        {"backend": "local"}
    )
    revoked_status, revoked_body = collector.revoke_content_analysis({})

    assert enabled_status == 207
    assert json.loads(enabled_body)["propagation"] == "failed"
    assert revoked_status == 503
    assert json.loads(revoked_body)["propagation"] == "failed"


def test_offline_registered_host_keeps_consent_and_revoke_partial(tmp_path) -> None:
    """Catches known offline hosts disappearing from fleet consent results."""

    collector = _make_collector(tmp_path)
    HarnessRegistry(collector.duckdb_path).register_host(
        host_id="offline-laptop",
        display_name="Offline Laptop",
        kind="macos",
        connection_kind="relay",
        status="offline",
    )
    collector.advisory_service = InsightsService(
        collector.duckdb_path, config_path=tmp_path / "config.toml"
    )

    enabled_status, enabled_body = collector.consent_content_analysis(
        {"backend": "local"}
    )
    revoked_status, revoked_body = collector.revoke_content_analysis({})

    assert enabled_status == 207
    assert json.loads(enabled_body)["hosts"] == [
        {"host_id": "offline-laptop", "state": "disconnected"}
    ]
    assert revoked_status == 207
    assert json.loads(revoked_body)["hosts"] == [
        {"host_id": "offline-laptop", "state": "disconnected"}
    ]


def test_empty_registry_is_complete_local_only_content_consent(tmp_path) -> None:
    """Defines no registered hosts as an intentional local-only deployment."""

    collector = _make_collector(tmp_path)
    collector.advisory_service = InsightsService(
        collector.duckdb_path, config_path=tmp_path / "config.toml"
    )

    enabled_status, enabled_body = collector.consent_content_analysis(
        {"backend": "local"}
    )
    revoked_status, revoked_body = collector.revoke_content_analysis({})

    assert enabled_status == 200
    assert json.loads(enabled_body)["propagation"] == "complete"
    assert json.loads(enabled_body)["hosts"] == []
    assert revoked_status == 200
    assert json.loads(revoked_body)["propagation"] == "complete"
    assert json.loads(revoked_body)["hosts"] == []


def test_harness_request_prefers_live_relay(collector_with_hosts) -> None:
    collector = collector_with_hosts
    fake = _FakeRelay()
    collector.relay_manager = fake
    host = collector._harness_host("laptop")
    status, body = collector._harness_request(host, "/sessions", method="GET")
    assert status == 200
    assert fake.calls == [("laptop", "GET", "/sessions", {})]


def test_fetch_advisory_content_bundle_uses_existing_relay_and_returns_bundle(
    collector_with_hosts, caplog
) -> None:
    collector = collector_with_hosts
    _enable_collector_content(collector)
    redacted_content = "private prompt body"
    content_hash = hashlib.sha256(redacted_content.encode("utf-8")).hexdigest()
    bundle_hash = hashlib.sha256(
        json.dumps(
            [["global-agents", content_hash]],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    class _BundleRelay(_FakeRelay):
        def request(
            self,
            host_id,
            method,
            path,
            body,
            timeout_s=15,
            max_response_bytes=None,
        ):
            self.calls.append((host_id, method, path, body))
            self.timeouts.append(timeout_s)
            self.max_response_bytes = max_response_bytes
            if path == "/advisory/content-consent":
                return 200, json.dumps(body)
            return 200, json.dumps(
                {
                    "bundle_hash": bundle_hash,
                    "created_at": "2026-08-08T12:00:00+00:00",
                    "targets": [
                        {
                            "target_id": "global-agents",
                            "content_hash": content_hash,
                            "redacted_content": redacted_content,
                        }
                    ],
                }
            )

    fake = _BundleRelay()
    collector.relay_manager = fake

    with caplog.at_level("INFO", logger="drover.metrics"):
        payload = collector.fetch_advisory_content_bundle("laptop", ["global-agents"])

    assert payload["bundle_hash"] == bundle_hash
    assert fake.calls == [
        (
            "laptop",
            "POST",
            "/advisory/content-consent",
            {"enabled": True, "epoch": 1},
        ),
        (
            "laptop",
            "POST",
            "/advisory/content-bundle",
            {"target_ids": ["global-agents"]},
        ),
    ]
    assert fake.max_response_bytes == metrics._MAX_CONTENT_BUNDLE_RESPONSE_BYTES
    assert "host=laptop" in caplog.text
    assert "targets=1" in caplog.text
    assert f"bundle_hash={bundle_hash}" in caplog.text
    assert "private prompt body" not in caplog.text


def test_fetch_advisory_content_version_uses_bounded_relay_hashes_only(
    collector_with_hosts,
) -> None:
    collector = collector_with_hosts
    _enable_collector_content(collector)
    content_hash = "a" * 64
    bundle_hash = hashlib.sha256(
        json.dumps(
            [["global-agents", content_hash]],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    class _VersionRelay(_FakeRelay):
        def request(
            self,
            host_id,
            method,
            path,
            body,
            timeout_s=15,
            max_response_bytes=None,
        ):
            self.calls.append((host_id, method, path, body))
            self.max_response_bytes = max_response_bytes
            if path == "/advisory/content-consent":
                return 200, json.dumps(body)
            return 200, json.dumps(
                {
                    "bundle_hash": bundle_hash,
                    "targets": [
                        {"target_id": "global-agents", "content_hash": content_hash}
                    ],
                }
            )

    fake = _VersionRelay()
    collector.relay_manager = fake

    payload = collector.fetch_advisory_content_version("laptop", ["global-agents"])

    assert payload == {
        "bundle_hash": bundle_hash,
        "targets": [{"target_id": "global-agents", "content_hash": content_hash}],
    }
    assert fake.calls[-1] == (
        "laptop",
        "POST",
        "/advisory/content-version",
        {"target_ids": ["global-agents"]},
    )
    assert fake.max_response_bytes == metrics._MAX_CONTENT_VERSION_RESPONSE_BYTES


@pytest.mark.parametrize(
    "payload",
    [
        {"bundle_hash": "a" * 64, "created_at": "not-a-time", "targets": []},
        {
            "bundle_hash": "a" * 64,
            "created_at": "2026-08-08T12:00:00+00:00",
            "targets": [
                {
                    "target_id": "different-target",
                    "content_hash": "b" * 64,
                    "redacted_content": "content",
                }
            ],
        },
        {
            "bundle_hash": "a" * 64,
            "created_at": "2026-08-08T12:00:00+00:00",
            "targets": [
                {
                    "target_id": "global-agents",
                    "content_hash": "b" * 64,
                    "redacted_content": "content does not match its hash",
                }
            ],
        },
    ],
)
def test_fetch_advisory_content_bundle_rejects_malformed_host_response(
    collector_with_hosts, payload
) -> None:
    collector = collector_with_hosts
    _enable_collector_content(collector)

    class _MalformedRelay(_FakeRelay):
        def request(
            self,
            host_id,
            method,
            path,
            body,
            timeout_s=15,
            max_response_bytes=None,
        ):
            if path == "/advisory/content-consent":
                return 200, json.dumps(body)
            return 200, json.dumps(payload)

    collector.relay_manager = _MalformedRelay()

    with pytest.raises(ValueError, match="content bundle response"):
        collector.fetch_advisory_content_bundle("laptop", ["global-agents"])


def test_fetch_advisory_content_bundle_rejects_oversized_relay_response(
    collector_with_hosts,
) -> None:
    collector = collector_with_hosts
    _enable_collector_content(collector)

    class _OversizedRelay(_FakeRelay):
        def request(
            self,
            host_id,
            method,
            path,
            body,
            timeout_s=15,
            max_response_bytes=None,
        ):
            if path == "/advisory/content-consent":
                return 200, json.dumps(body)
            return 200, "x" * (4 * 1024 * 1024 + 1)

    collector.relay_manager = _OversizedRelay()

    with pytest.raises(ValueError, match="exceeds byte limit"):
        collector.fetch_advisory_content_bundle("laptop", ["global-agents"])


@pytest.mark.parametrize(
    "target_ids",
    [[], ["global-agents", "global-agents"], ["../AGENTS.md"], ["global-agents", 1]],
)
def test_fetch_advisory_content_bundle_rejects_invalid_target_ids_before_transport(
    collector_with_hosts, target_ids
) -> None:
    collector = collector_with_hosts
    fake = _FakeRelay()
    collector.relay_manager = fake

    with pytest.raises(ValueError, match="target_ids"):
        collector.fetch_advisory_content_bundle("laptop", target_ids)

    assert fake.calls == []


def test_proxy_harness_request_rejects_large_content_length_without_reading(
    collector_with_hosts, monkeypatch
) -> None:
    collector = collector_with_hosts

    class _Response:
        status = 200

        def getheader(self, name):
            return str(4 * 1024 * 1024 + 1) if name == "Content-Length" else None

        def read(self, amount=None):
            raise AssertionError("oversized response body was read")

    class _Connection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return _Response()

        def close(self):
            pass

    monkeypatch.setattr(metrics.http.client, "HTTPConnection", _Connection)

    status, body = collector._proxy_harness_request(
        "http://127.0.0.1/advisory/content-bundle",
        method="POST",
        payload={"target_ids": ["global-agents"]},
        max_response_bytes=4 * 1024 * 1024,
    )

    assert status == 502
    assert "exceeds byte limit" in body


def test_harness_request_falls_back_to_direct_url(collector_with_hosts) -> None:
    collector = collector_with_hosts
    collector.relay_manager = _FakeRelay()  # not live for "mini"
    host = collector._harness_host("mini")
    status, _ = collector._harness_request(
        host, "/sessions", method="GET", timeout_s=0.2
    )
    assert (
        status == 502
    )  # tried the direct URL and it refused -- proves the direct path ran


def test_harness_request_no_endpoint_no_relay_is_502(collector_with_hosts) -> None:
    collector = collector_with_hosts
    host = collector._harness_host("island")
    status, body = collector._harness_request(host, "/sessions", method="GET")
    assert status == 502
    assert "no reachable endpoint" in body


def test_harness_request_raises_tight_budgets_over_a_relay(
    collector_with_hosts,
) -> None:
    """LAN-shaped budgets are not budgets at all over a funnel from cellular.

    The 1.0s reconcile and 2.0s transcript budgets were chosen for a LAN dial.
    Over a relay they expire while the spoke's loopback call is still running,
    the hub 502s, and -- the transcript endpoint being polled -- each expiry
    leaves another orphaned thread on the laptop.
    """
    collector = collector_with_hosts
    fake = _FakeRelay()
    collector.relay_manager = fake
    host = collector._harness_host("laptop")

    collector._harness_request(host, "/sessions/s1", method="GET", timeout_s=1.0)
    collector._harness_request(host, "/transcript", method="GET", timeout_s=2.0)
    # A caller asking for more than the floor keeps its own budget.
    collector._harness_request(host, "/slow", method="GET", timeout_s=30.0)

    assert fake.timeouts == [
        metrics.RELAY_MIN_TIMEOUT_S,
        metrics.RELAY_MIN_TIMEOUT_S,
        30.0,
    ]


def test_harness_request_never_dials_a_relay_host_by_url(collector_with_hosts) -> None:
    """A relay host with a stray URL must 502, not dial it.

    The danger is not a wasted request. Every host shape in this repo listens
    on 127.0.0.1:7081 by default, so a local_url that ends up on a relay row --
    a stale manual test, someone copying the direct docs, a future enroll
    change -- resolves against the *hub's own loopback*. The hub would then
    run the work laptop's session commands against its own harnessd, silently
    and with no error, on sessions that have filesystem access.

    So this stands up a real listener and proves nothing ever reaches it.
    """
    collector = collector_with_hosts
    hits: list[str] = []

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            hits.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args: object) -> None:
            pass

    decoy = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=decoy.serve_forever, daemon=True)
    thread.start()
    try:
        HarnessRegistry(collector.duckdb_path).register_host(
            host_id="laptop",
            display_name="Laptop",
            kind="macos",
            connection_kind="relay",
            local_url=f"http://127.0.0.1:{decoy.server_address[1]}",
        )
        collector.relay_manager = None  # no live socket for "laptop"
        host = collector._harness_host("laptop")
        assert host.local_url  # the stray URL really is on the row
        status, body = collector._harness_request(host, "/sessions", method="GET")
    finally:
        decoy.shutdown()
        decoy.server_close()
        thread.join(timeout=5)

    assert status == 502
    assert "not connected" in body
    assert hits == [], f"hub dialled a relay host's URL: {hits}"


def test_fleet_json_overrides_relay_status_from_socket(collector_with_hosts) -> None:
    # Top-level key confirmed by reading render_harness_json / harness_snapshot:
    # it emits {"hosts": [...], "sessions": [...], "cwd_suggestions": [...]}.
    collector = collector_with_hosts  # "laptop" registered connection_kind="relay", status "online"
    # Second relay host, stored "online", but _FakeRelay.is_live only recognizes
    # "laptop" -- this is the realistic production case: hub up, some other
    # relay host's socket has dropped. Registered directly against the
    # fixture's duckdb so we don't disturb collector_with_hosts for other tests.
    registry = HarnessRegistry(collector.duckdb_path)
    registry.register_host(
        host_id="phone",
        display_name="Phone",
        kind="ios",
        connection_kind="relay",
    )
    collector.relay_manager = _FakeRelay()  # is_live: only "laptop"

    payload = json.loads(collector.render_harness_json(include_sessions=False))
    hosts = {h["host_id"]: h for h in payload["hosts"]}
    assert hosts["laptop"]["connection_kind"] == "relay"
    assert hosts["laptop"]["status"] == "online"
    assert hosts["phone"]["connection_kind"] == "relay"
    assert hosts["phone"]["status"] == "offline"  # stored "online" must not leak

    # Same override holds when sessions are included in the render.
    payload = json.loads(collector.render_harness_json(include_sessions=True))
    hosts = {h["host_id"]: h for h in payload["hosts"]}
    assert hosts["laptop"]["status"] == "online"
    assert hosts["phone"]["status"] == "offline"

    collector.relay_manager = None  # no live sockets at all
    payload = json.loads(collector.render_harness_json(include_sessions=False))
    hosts = {h["host_id"]: h for h in payload["hosts"]}
    assert hosts["laptop"]["status"] == "offline"  # stored "online" must not leak
    assert hosts["phone"]["status"] == "offline"
    assert hosts["mini"]["status"] == "online"  # direct host keeps stored status


def test_proxy_forwards_bearer_to_harnessd(tmp_path):
    _FakeHarnessHandler.requests = []
    harness_server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeHarnessHandler)
    harness_thread = metrics.threading.Thread(
        target=harness_server.serve_forever, daemon=True
    )
    harness_thread.start()
    harness_port = harness_server.server_address[1]

    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    registry = HarnessRegistry(duckdb_path)
    registry.register_host(
        host_id="mac-mini",
        display_name="Mac Mini",
        kind="macos",
        local_url=f"http://127.0.0.1:{harness_port}",
        capabilities={"harnesses": [{"name": "shell", "enabled": True}]},
    )
    collector = MetricsCollector(
        duckdb_path=duckdb_path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        ttl_seconds=60,
        api_token="host-secret",
    )
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        port = server.server_address[1]
        launch = Request(
            f"http://127.0.0.1:{port}/harness/hosts/mac-mini/sessions",
            data=json.dumps({"harness": "shell", "cwd": "/tmp"}).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", **_AUTH_HEADERS},
        )
        with urlopen(launch, timeout=3) as res:
            json.loads(res.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()
        harness_server.shutdown()
        harness_server.server_close()

    assert _FakeHarnessHandler.requests[0]["authorization"] == "Bearer host-secret"


def test_metrics_http_server_proxies_native_session_discovery(tmp_path):
    _FakeHarnessHandler.requests = []
    harness_server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeHarnessHandler)
    harness_thread = metrics.threading.Thread(
        target=harness_server.serve_forever, daemon=True
    )
    harness_thread.start()
    harness_port = harness_server.server_address[1]

    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    registry = HarnessRegistry(duckdb_path)
    registry.register_host(
        host_id="mac-mini",
        display_name="Mac Mini",
        kind="macos",
        local_url=f"http://127.0.0.1:{harness_port}",
        capabilities={"harnesses": [{"name": "codex", "enabled": True}]},
    )
    collector = MetricsCollector(
        duckdb_path=duckdb_path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        ttl_seconds=60,
    )
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        port = server.server_address[1]
        with _authed_get(
            "http://127.0.0.1:"
            f"{port}/harness/hosts/mac-mini/native-sessions"
            "?harness=codex&cwd=%2Ftmp%2Fnexus&limit=5",
        ) as res:
            payload = json.loads(res.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()
        harness_server.shutdown()
        harness_server.server_close()

    assert payload["sessions"][0]["session_id"] == "codex-native-1"
    assert _FakeHarnessHandler.requests[0]["path"] == (
        "/native-sessions?harness=codex&cwd=%2Ftmp%2Fnexus&limit=5"
    )


def test_metrics_http_server_proxies_native_transcript(tmp_path):
    _FakeHarnessHandler.requests = []
    harness_server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeHarnessHandler)
    harness_thread = metrics.threading.Thread(
        target=harness_server.serve_forever, daemon=True
    )
    harness_thread.start()
    harness_port = harness_server.server_address[1]
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    registry = HarnessRegistry(duckdb_path)
    registry.register_host(
        host_id="mac-mini",
        display_name="Mac Mini",
        kind="macos",
        local_url=f"http://127.0.0.1:{harness_port}",
        capabilities={"harnesses": [{"name": "claude-code", "enabled": True}]},
    )
    registry.create_session(
        session_id="harness-running",
        host_id="mac-mini",
        harness="claude-code",
        command="claude-code",
        cwd="/tmp/nexus",
        status="running",
        native_session_id="claude-native-1",
    )
    collector = MetricsCollector(
        duckdb_path=duckdb_path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        ttl_seconds=60,
    )
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        port = server.server_address[1]
        with _authed_get(
            f"http://127.0.0.1:{port}/harness/sessions/harness-running/native-transcript",
        ) as res:
            payload = json.loads(res.read().decode("utf-8"))
        with _authed_get(
            f"http://127.0.0.1:{port}/harness/sessions/harness-running",
        ) as res:
            snapshot = json.loads(res.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()
        harness_server.shutdown()
        harness_server.server_close()

    assert payload["source"] == "claude jsonl"
    assert payload["messages"][1]["text"] == "Nexus is a local-first context store."
    assert (
        snapshot["native_transcript"]["messages"][0]["text"]
        == "Summarise this project."
    )
    assert any(
        request["path"]
        == "/sessions/harness-running/native-transcript?native_session_id=claude-native-1&limit=100"
        for request in _FakeHarnessHandler.requests
    )


def test_metrics_http_server_continues_session_with_nexus_handoff(tmp_path):
    _FakeHarnessHandler.requests = []
    harness_server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeHarnessHandler)
    harness_thread = metrics.threading.Thread(
        target=harness_server.serve_forever, daemon=True
    )
    harness_thread.start()
    harness_port = harness_server.server_address[1]

    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    registry = HarnessRegistry(duckdb_path)
    registry.register_host(
        host_id="mac-mini",
        display_name="Mac Mini",
        kind="macos",
        local_url=f"http://127.0.0.1:{harness_port}",
        capabilities={
            "harnesses": [
                {"name": "claude-code", "enabled": True},
                {"name": "codex", "enabled": True},
            ]
        },
    )
    source = registry.create_session(
        session_id="harness-source",
        host_id="mac-mini",
        harness="claude-code",
        command="claude",
        status="running",
        cwd="/Users/arnabmac/jenny/nexus",
        repo_owner="arniesaha",
        repo_name="nexus",
        branch="main",
    )
    registry.append_event(
        session_id=source.session_id,
        event_type="terminal.output",
        payload={"text": "We just implemented central host heartbeats.\n"},
    )
    collector = MetricsCollector(
        duckdb_path=duckdb_path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        ttl_seconds=60,
    )
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        port = server.server_address[1]
        request = Request(
            f"http://127.0.0.1:{port}/harness/sessions/{source.session_id}/continue",
            data=json.dumps(
                {"target_host_id": "mac-mini", "target_harness": "codex"}
            ).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", **_AUTH_HEADERS},
        )
        with urlopen(request, timeout=3) as res:
            payload = json.loads(res.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()
        harness_server.shutdown()
        harness_server.server_close()

    assert payload["session_id"] == "harness-proxied"
    # Structured-capable target: the client needs mode=structured in the
    # response body to navigate to the structured session UI.
    assert payload["mode"] == "structured"
    launch = _FakeHarnessHandler.requests[0]["body"]
    assert launch["harness"] == "codex"
    assert launch["cwd"] == "/Users/arnabmac/jenny/nexus"
    assert launch["source_session_id"] == "harness-source"
    assert launch["handoff_mode"] == "nexus_handoff"
    # Handoff to a structured-capable harness launches a structured session
    # and delivers the handoff text as the first turn ("prompt"), never as a
    # typed PTY seed ("initial_input") racing the CLI's cold start.
    assert launch["mode"] == "structured"
    assert "initial_input" not in launch
    assert "rows" not in launch
    assert "cols" not in launch
    assert "Continue this Drover Harness session" in launch["prompt"]
    assert "We just implemented central host heartbeats." in launch["prompt"]
    created = HarnessRegistry(duckdb_path).get_session("harness-proxied")
    assert created is not None
    assert created.source_session_id == "harness-source"
    assert created.handoff_mode == "nexus_handoff"
    assert created.cwd == "/Users/arnabmac/jenny/nexus"
    assert created.mode == "structured"


def test_metrics_http_server_continue_to_shell_target_keeps_pty_seed(tmp_path):
    _FakeHarnessHandler.requests = []
    harness_server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeHarnessHandler)
    harness_thread = metrics.threading.Thread(
        target=harness_server.serve_forever, daemon=True
    )
    harness_thread.start()
    harness_port = harness_server.server_address[1]

    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    registry = HarnessRegistry(duckdb_path)
    registry.register_host(
        host_id="mac-mini",
        display_name="Mac Mini",
        kind="macos",
        local_url=f"http://127.0.0.1:{harness_port}",
        capabilities={
            "harnesses": [
                {"name": "claude-code", "enabled": True},
                {"name": "shell", "enabled": True},
            ]
        },
    )
    source = registry.create_session(
        session_id="harness-source",
        host_id="mac-mini",
        harness="claude-code",
        command="claude",
        status="running",
        cwd="/Users/arnabmac/jenny/nexus",
    )
    collector = MetricsCollector(
        duckdb_path=duckdb_path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        ttl_seconds=60,
    )
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        port = server.server_address[1]
        request = Request(
            f"http://127.0.0.1:{port}/harness/sessions/{source.session_id}/continue",
            data=json.dumps(
                {"target_host_id": "mac-mini", "target_harness": "shell"}
            ).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", **_AUTH_HEADERS},
        )
        with urlopen(request, timeout=3) as res:
            payload = json.loads(res.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()
        harness_server.shutdown()
        harness_server.server_close()

    assert payload["session_id"] == "harness-proxied"
    assert "mode" not in payload
    launch = _FakeHarnessHandler.requests[0]["body"]
    assert launch["harness"] == "shell"
    assert launch["handoff_mode"] == "nexus_handoff"
    # Shell has no structured driver: the handoff still goes through the PTY
    # typed-seed path.
    assert "mode" not in launch
    assert "prompt" not in launch
    assert launch["rows"] == 32
    assert launch["cols"] == 100
    assert "Continue this Drover Harness session" in launch["initial_input"]


def test_metrics_http_server_continues_session_with_native_resume(tmp_path):
    _FakeHarnessHandler.requests = []
    harness_server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeHarnessHandler)
    harness_thread = metrics.threading.Thread(
        target=harness_server.serve_forever, daemon=True
    )
    harness_thread.start()
    harness_port = harness_server.server_address[1]

    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    registry = HarnessRegistry(duckdb_path)
    registry.register_host(
        host_id="mac-mini",
        display_name="Mac Mini",
        kind="macos",
        local_url=f"http://127.0.0.1:{harness_port}",
        capabilities={"harnesses": [{"name": "claude-code", "enabled": True}]},
    )
    source = registry.create_session(
        session_id="harness-source",
        host_id="mac-mini",
        harness="claude-code",
        command="claude",
        status="running",
        cwd="/Users/arnabmac/jenny/nexus",
    )
    collector = MetricsCollector(
        duckdb_path=duckdb_path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        ttl_seconds=60,
    )
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        port = server.server_address[1]
        request = Request(
            f"http://127.0.0.1:{port}/harness/sessions/{source.session_id}/continue",
            data=json.dumps(
                {
                    "target_host_id": "mac-mini",
                    "target_harness": "claude-code",
                    "native_resume": {
                        "session_id": "claude-native-1",
                        "label": "latest Claude work",
                    },
                }
            ).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", **_AUTH_HEADERS},
        )
        with urlopen(request, timeout=3) as res:
            payload = json.loads(res.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()
        harness_server.shutdown()
        harness_server.server_close()

    assert payload["session_id"] == "harness-proxied"
    launch = _FakeHarnessHandler.requests[0]["body"]
    assert launch["harness"] == "claude-code"
    assert launch["handoff_mode"] == "native_resume"
    assert launch["native_resume"]["session_id"] == "claude-native-1"
    assert "initial_input" not in launch
    # Native resume stays on the PTY path: the harness CLI replays its own
    # native session, so no structured first-turn prompt is involved.
    assert "mode" not in launch
    assert "prompt" not in launch
    created = HarnessRegistry(duckdb_path).get_session("harness-proxied")
    assert created is not None
    assert created.native_session_id == "claude-native-1"
    assert created.native_resume_label == "latest Claude work"
    assert created.handoff_mode == "native_resume"


def test_metrics_http_server_relays_harness_terminal_websocket(tmp_path):
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    registry = HarnessRegistry(duckdb_path)
    state = HarnessDaemonState(
        host_id="mac-mini",
        display_name="Mac Mini",
        kind="macos",
        registry=registry,
        pty=PtySessionManager(),
        presets=DEFAULT_PRESETS,
        local_url="http://127.0.0.1:0",
    )
    register_daemon_host(state)
    harness_server = create_harness_server(
        listen_host="127.0.0.1",
        listen_port=0,
        state=state,
    )
    harness_thread = metrics.threading.Thread(
        target=harness_server.serve_forever, daemon=True
    )
    harness_thread.start()
    harness_port = harness_server.server_address[1]
    registry.register_host(
        host_id="mac-mini",
        display_name="Mac Mini",
        kind="macos",
        local_url=f"http://127.0.0.1:{harness_port}",
        capabilities={"harnesses": [{"name": "shell", "enabled": True}]},
    )
    collector = MetricsCollector(
        duckdb_path=duckdb_path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        ttl_seconds=60,
    )
    metrics_server = start_metrics_server(host="127.0.0.1", port=0, collector=collector)
    sock = None
    try:
        _, created = _json_request(
            f"http://127.0.0.1:{harness_port}/sessions",
            payload={"harness": "shell"},
        )
        session_id = created["session_id"]
        metrics_port = metrics_server.server_address[1]
        sock = _connect_ws(
            f"http://127.0.0.1:{metrics_port}",
            f"/harness/sessions/{session_id}/terminal",
        )
        attached = _recv_json(sock)
        client_send_json(sock, {"type": "input", "data": "echo RELAY_OK\n"})
        output = _wait_for_output(sock, "RELAY_OK")
    finally:
        if sock is not None:
            sock.close()
        metrics_server.shutdown()
        metrics_server.server_close()
        harness_server.shutdown()
        harness_server.server_close()

    assert attached["type"] == "attached"
    assert "RELAY_OK" in output


def test_metrics_http_server_mirrors_proxied_harness_events(tmp_path):
    host_duckdb_path = tmp_path / "host-drover.duckdb"
    central_duckdb_path = tmp_path / "central-drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "host-parquet", duckdb_path=host_duckdb_path)
    bootstrap(parquet_dir=tmp_path / "central-parquet", duckdb_path=central_duckdb_path)
    host_registry = HarnessRegistry(host_duckdb_path)
    state = HarnessDaemonState(
        host_id="nas",
        display_name="NAS",
        kind="linux",
        registry=host_registry,
        pty=PtySessionManager(),
        presets=DEFAULT_PRESETS,
        local_url="http://127.0.0.1:0",
    )
    register_daemon_host(state)
    harness_server = create_harness_server(
        listen_host="127.0.0.1",
        listen_port=0,
        state=state,
    )
    harness_thread = metrics.threading.Thread(
        target=harness_server.serve_forever, daemon=True
    )
    harness_thread.start()
    harness_port = harness_server.server_address[1]
    central_registry = HarnessRegistry(central_duckdb_path)
    central_registry.register_host(
        host_id="nas",
        display_name="NAS",
        kind="linux",
        local_url=f"http://127.0.0.1:{harness_port}",
        capabilities={"harnesses": [{"name": "shell", "enabled": True}]},
    )
    collector = MetricsCollector(
        duckdb_path=central_duckdb_path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        ttl_seconds=60,
    )
    metrics_server = start_metrics_server(host="127.0.0.1", port=0, collector=collector)
    sock = None
    try:
        _, created = _json_request(
            f"http://127.0.0.1:{harness_port}/sessions",
            payload={"harness": "shell"},
        )
        session_id = created["session_id"]
        central_registry.create_session(
            session_id=session_id,
            host_id="nas",
            harness="shell",
            command="/bin/sh",
            status="running",
        )
        metrics_port = metrics_server.server_address[1]
        sock = _connect_ws(
            f"http://127.0.0.1:{metrics_port}",
            f"/harness/sessions/{session_id}/terminal",
        )
        _recv_json(sock)
        client_send_json(sock, {"type": "input", "data": "echo MIRROR_OK\n"})
        assert "MIRROR_OK" in _wait_for_output(sock, "MIRROR_OK")
    finally:
        if sock is not None:
            sock.close()
        metrics_server.shutdown()
        metrics_server.server_close()
        harness_server.shutdown()
        harness_server.server_close()

    mirrored = HarnessRegistry(central_duckdb_path).list_events(session_id)
    normalized = {event.event_type: event for event in mirrored}
    assert normalized["terminal.input"].normalized_type == "command"
    assert normalized["terminal.input"].normalized_source == "inferred_terminal"
    assert normalized["terminal.output"].content_preview


def test_harness_session_snapshot_reconciles_stale_running_host_session(tmp_path):
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    registry = HarnessRegistry(duckdb_path)
    fake = ThreadingHTTPServer(("127.0.0.1", 0), _FakeHarnessHandler)
    thread = metrics.threading.Thread(target=fake.serve_forever, daemon=True)
    thread.start()
    port = fake.server_address[1]
    registry.register_host(
        host_id="nas",
        display_name="NAS",
        kind="linux",
        local_url=f"http://127.0.0.1:{port}",
        capabilities={"harnesses": [{"name": "openclaw", "enabled": True}]},
    )
    registry.create_session(
        session_id="harness-stale-openclaw",
        host_id="nas",
        harness="openclaw",
        command="openclaw",
        status="running",
    )
    collector = MetricsCollector(
        duckdb_path=duckdb_path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        ttl_seconds=60,
    )
    try:
        snapshot = collector.harness_session_snapshot("harness-stale-openclaw")
    finally:
        fake.shutdown()
        fake.server_close()

    session = snapshot["session"]
    assert session["status"] == "completed"
    assert "host no longer has active PTY session" in session["last_error"]
    assert collector.harness_terminal_endpoint("harness-stale-openclaw") is None


def test_auth_rejects_missing_token(tmp_path):
    collector = _make_collector(tmp_path)
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        port = server.server_address[1]
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/harness", timeout=5)
        assert exc.value.code == 401
        assert "authentication required" in exc.value.read().decode()
    finally:
        server.shutdown()


def test_auth_healthz_open(tmp_path):
    collector = _make_collector(tmp_path)
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        port = server.server_address[1]
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/healthz", timeout=5
        ) as res:
            status = res.status
            body = res.read().decode("utf-8")
    finally:
        server.shutdown()

    assert status == 200
    assert body == "ok\n"


def test_auth_accepts_bearer(tmp_path):
    collector = _make_collector(tmp_path)
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        port = server.server_address[1]
        with _authed_get(f"http://127.0.0.1:{port}/harness") as res:
            status = res.status
            body = res.read().decode("utf-8")
    finally:
        server.shutdown()

    assert status == 200
    json.loads(body)  # valid JSON body


def test_auth_accepts_session_cookie(tmp_path):
    collector = _make_collector(tmp_path)
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        port = server.server_address[1]
        session_value = mint_session(_TEST_AUTH)
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/harness",
            headers={"Cookie": f"drover_session={session_value}"},
        )
        with urllib.request.urlopen(request, timeout=5) as res:
            status = res.status
    finally:
        server.shutdown()

    assert status == 200


def test_auth_ui_redirects_to_login(tmp_path):
    collector = _make_collector(tmp_path)
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        port = server.server_address[1]

        opener = urllib.request.build_opener(_NoRedirect)
        with pytest.raises(urllib.error.HTTPError) as exc:
            opener.open(f"http://127.0.0.1:{port}/ui", timeout=5)
        assert exc.value.code == 302
        assert exc.value.headers["Location"] == "/auth/login"
    finally:
        server.shutdown()


def test_login_page_is_public(tmp_path):
    collector = _make_collector(tmp_path)
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        port = server.server_address[1]
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/auth/login", timeout=5
        ) as res:
            status = res.status
            body = res.read().decode("utf-8")
    finally:
        server.shutdown()

    assert status == 200
    assert "<form" in body


def test_login_success_sets_cookie_and_cookie_works(tmp_path):
    collector = _make_collector(tmp_path)
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        port = server.server_address[1]
        opener = urllib.request.build_opener(_NoRedirect)
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/auth/login",
            data=f"token={_TEST_TOKEN}".encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            opener.open(request, timeout=5)
        assert exc.value.code == 302
        assert exc.value.headers["Location"] == "/ui"
        set_cookie = exc.value.headers["Set-Cookie"]
        assert set_cookie is not None
        cookie_value = set_cookie.split(";", 1)[0]

        cookie_request = urllib.request.Request(
            f"http://127.0.0.1:{port}/harness",
            headers={"Cookie": cookie_value},
        )
        with urllib.request.urlopen(cookie_request, timeout=5) as res:
            status = res.status
    finally:
        server.shutdown()

    assert status == 200


def test_harness_events_ingest_idempotent(tmp_path):
    collector = _make_collector(tmp_path)
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        port = server.server_address[1]
        event = {
            "event_id": "harness-event-x1",
            "session_id": "harness-s1",
            "seq": 1,
            "type": "assistant_output",
            "role": "assistant",
            "text": "hi",
            "payload": {},
            "turn_id": None,
            "ts": "2026-07-06T00:00:00+00:00",
        }
        for expected_new in (1, 0):  # second POST is a replay
            status, body = _json_request(
                f"http://127.0.0.1:{port}/harness/events",
                payload={"events": [event]},
            )
            assert status == 200
            assert body["ingested"] == expected_new
    finally:
        server.shutdown()


def test_harness_events_ingest_requires_auth(tmp_path):
    collector = _make_collector(tmp_path)
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        port = server.server_address[1]
        event = {
            "event_id": "harness-event-x2",
            "session_id": "harness-s1",
            "seq": 1,
            "type": "assistant_output",
            "payload": {},
        }
        request = Request(
            f"http://127.0.0.1:{port}/harness/events",
            data=json.dumps({"events": [event]}).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urlopen(request, timeout=5)
        assert exc.value.code == 401
    finally:
        server.shutdown()


def test_harness_events_ingest_rejects_malformed_body(tmp_path):
    collector = _make_collector(tmp_path)
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        port = server.server_address[1]
        with pytest.raises(urllib.error.HTTPError) as exc:
            _json_request(
                f"http://127.0.0.1:{port}/harness/events",
                payload={"events": [{"event_id": "only-id"}]},
            )
        assert exc.value.code == 400
    finally:
        server.shutdown()


def test_ingest_harness_events_derives_session_awaiting_state(tmp_path):
    collector = _make_collector(tmp_path)
    registry = HarnessRegistry(collector.duckdb_path)
    registry.register_host(host_id="nas", display_name="NAS", kind="linux")
    registry.create_session(
        host_id="nas",
        harness="claude-code",
        command="claude",
        session_id="harness-s3",
        status="running",
        mode="structured",
    )
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        port = server.server_address[1]
        _ingest_events(
            port,
            [
                {
                    "event_id": "harness-event-a1",
                    "session_id": "harness-s3",
                    "seq": 1,
                    "type": "status",
                    "payload": {"native_session_id": "provider-session-3"},
                    "ts": "2026-07-06T00:00:01+00:00",
                },
                {
                    "event_id": "harness-event-a2",
                    "session_id": "harness-s3",
                    "seq": 2,
                    "type": "approval_prompt",
                    "payload": {"request_id": "req-1"},
                    "ts": "2026-07-06T00:00:02+00:00",
                },
            ],
        )
        session = registry.get_session("harness-s3")
        assert session.awaiting == "approval"
        assert session.last_activity is not None
        assert session.native_session_id == "provider-session-3"

        _ingest_events(
            port,
            [
                {
                    "event_id": "harness-event-a3",
                    "session_id": "harness-s3",
                    "seq": 3,
                    "type": "approval_response",
                    "payload": {"request_id": "req-1", "decision": "allow"},
                    "ts": "2026-07-06T00:00:03+00:00",
                },
                {
                    "event_id": "harness-event-a4",
                    "session_id": "harness-s3",
                    "seq": 4,
                    "type": "assistant_output",
                    "payload": {},
                    "ts": "2026-07-06T00:00:04+00:00",
                },
                {
                    "event_id": "harness-event-a5",
                    "session_id": "harness-s3",
                    "seq": 5,
                    "type": "status",
                    "payload": {"turn_complete": True, "awaiting": "input"},
                    "ts": "2026-07-06T00:00:05+00:00",
                },
            ],
        )
        session = registry.get_session("harness-s3")
        assert session.awaiting == "input"
    finally:
        server.shutdown()


def test_login_wrong_token_redirects_with_error(tmp_path):
    collector = _make_collector(tmp_path)
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        port = server.server_address[1]
        opener = urllib.request.build_opener(_NoRedirect)
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/auth/login",
            data=b"token=wrong",
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            opener.open(request, timeout=5)
        assert exc.value.code == 302
        assert exc.value.headers["Location"] == "/auth/login?error=1"
        assert exc.value.headers.get("Set-Cookie") is None
    finally:
        server.shutdown()


def _ingest_events(port: int, events: list[dict]) -> None:
    status, _ = _json_request(
        f"http://127.0.0.1:{port}/harness/events", payload={"events": events}
    )
    assert status == 200


def test_harness_events_wire_completion_enqueues_recap_at_host_sequence(tmp_path):
    collector = _make_collector(tmp_path)
    registry = HarnessRegistry(collector.duckdb_path)
    registry.create_session(
        host_id="nas",
        harness="codex",
        command="codex",
        session_id="harness-recap-wire",
        status="running",
        mode="structured",
    )
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    event = {
        "event_id": "harness-event-recap-wire-12",
        "session_id": "harness-recap-wire",
        "seq": 12,
        "type": "status",
        "role": "system",
        "text": "turn complete",
        "payload": {"turn_complete": True, "awaiting": "input"},
        "turn_id": "turn-1",
        "ts": "2026-07-06T00:00:12+00:00",
    }
    try:
        port = server.server_address[1]
        status, body = _json_request(
            f"http://127.0.0.1:{port}/harness/events",
            payload={"events": [event]},
        )
        assert status == 200
        assert body == {"ingested": 1}
    finally:
        server.shutdown()

    with duckdb.connect(str(control_plane_path(collector.duckdb_path))) as con:
        assert con.execute(
            "SELECT desired_source_seq FROM live_recap_jobs WHERE session_id = ?",
            ["harness-recap-wire"],
        ).fetchone() == (12,)


def test_failed_split_db_sync_keeps_session_and_worker_retries_recap_once(
    tmp_path, monkeypatch
):
    """Removing the worker orphan-reconcile poll permanently loses the newest recap."""
    collector = _make_collector(tmp_path)
    registry = HarnessRegistry(collector.duckdb_path)
    event = {
        "event_id": "harness-event-race-23",
        "session_id": "harness-race",
        "seq": 23,
        "type": "status",
        "role": "system",
        "text": "turn complete",
        "payload": {"turn_complete": True, "awaiting": "input"},
        "turn_id": "turn-race",
        "ts": "2026-07-06T00:00:23+00:00",
    }
    older_event = {
        "event_id": "harness-event-race-7",
        "session_id": "harness-race",
        "seq": 7,
        "type": "status",
        "role": "system",
        "text": "older turn complete",
        "payload": {"turn_complete": True, "awaiting": "input"},
        "turn_id": "turn-older",
        "ts": "2026-07-06T00:00:07+00:00",
    }
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        port = server.server_address[1]
        status, body = _json_request(
            f"http://127.0.0.1:{port}/harness/events",
            payload={"events": [older_event, event]},
        )
        assert status == 200
        assert body == {"ingested": 2}
        with duckdb.connect(str(control_plane_path(collector.duckdb_path))) as con:
            assert con.execute(
                "SELECT count(*) FROM live_recap_jobs WHERE session_id = ?",
                ["harness-race"],
            ).fetchone() == (0,)

        response_body = json.dumps(
            {
                "session_id": "harness-race",
                "harness": "codex",
                "command": ["codex"],
                "status": "running",
                "mode": "structured",
            }
        )
        real_enqueue = registry_module.enqueue_live_recap
        attempts = 0

        def unavailable_once(con, session_id, source_seq):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("queue temporarily unavailable")
            return real_enqueue(con, session_id, source_seq)

        monkeypatch.setattr(registry_module, "enqueue_live_recap", unavailable_once)
        collector._sync_created_harness_session(
            "nas", {"harness": "codex", "mode": "structured"}, response_body
        )

        assert registry.get_session("harness-race") is not None
        with duckdb.connect(str(control_plane_path(collector.duckdb_path))) as con:
            assert con.execute(
                "SELECT count(*) FROM live_recap_jobs WHERE session_id = ?",
                ["harness-race"],
            ).fetchone() == (0,)
            assert con.execute(
                "SELECT recap_reconcile_needed FROM harness_sessions "
                "WHERE session_id = ?",
                ["harness-race"],
            ).fetchone() == (True,)

        # drain_once is the production worker loop's scheduled retry boundary.
        # No backend is needed to prove queue recovery: generation moves the
        # recovered row to retry_wait after reporting its missing backend.
        assert LiveRecapWorker(duckdb_path=collector.duckdb_path).drain_once() == 1
    finally:
        server.shutdown()

    with duckdb.connect(str(control_plane_path(collector.duckdb_path))) as con:
        marker_after_recovery = con.execute(
            "SELECT recap_reconcile_needed FROM harness_sessions "
            "WHERE session_id = ?",
            ["harness-race"],
        ).fetchone()
        assert con.execute(
            "SELECT desired_source_seq, status, count(*) FROM live_recap_jobs "
            "WHERE session_id = ? GROUP BY desired_source_seq, status",
            ["harness-race"],
        ).fetchone() == (23, "retry_wait", 1)
        # Remove ordinary due-job work from the second poll. If reconciliation
        # cleanup were missing, the still-marked session would recreate this
        # row from the stored completion and drain_once would handle it again.
        con.execute(
            "DELETE FROM live_recap_jobs WHERE session_id = ?", ["harness-race"]
        )

    assert LiveRecapWorker(duckdb_path=collector.duckdb_path).drain_once() == 0
    assert marker_after_recovery == (False,)
    with duckdb.connect(str(control_plane_path(collector.duckdb_path))) as con:
        assert con.execute(
            "SELECT recap_reconcile_needed FROM harness_sessions "
            "WHERE session_id = ?",
            ["harness-race"],
        ).fetchone() == (False,)
        assert con.execute(
            "SELECT count(*) FROM live_recap_jobs WHERE session_id = ?",
            ["harness-race"],
        ).fetchone() == (0,)


def _event(seq: int, text: str) -> dict:
    return {
        "event_id": f"harness-event-m{seq}",
        "session_id": "harness-s2",
        "seq": seq,
        "type": "assistant_output",
        "role": "assistant",
        "text": text,
        "payload": {},
        "turn_id": None,
        "ts": "2026-07-06T00:00:00+00:00",
    }


def test_messages_endpoint_orders_and_filters_by_seq(tmp_path):
    collector = _make_collector(tmp_path)
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        port = server.server_address[1]
        _ingest_events(port, [_event(2, "b"), _event(1, "a"), _event(3, "c")])
        with _authed_get(
            f"http://127.0.0.1:{port}/harness/sessions/harness-s2/messages"
            "?after_seq=1"
        ) as response:
            body = json.loads(response.read())
        assert [m["text"] for m in body["messages"]] == ["b", "c"]
        assert body["max_seq"] == 3
    finally:
        server.shutdown()


def test_messages_endpoint_overlays_canonical_event_metadata(tmp_path):
    collector = _make_collector(tmp_path)
    HarnessRegistry(collector.duckdb_path).append_event(
        event_id="legacy-e1",
        session_id="legacy",
        seq=1,
        event_type="assistant_output",
        payload={"text": "hello"},
    )
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        port = server.server_address[1]
        with _authed_get(
            f"http://127.0.0.1:{port}/harness/sessions/legacy/messages"
        ) as response:
            body = json.loads(response.read())
        assert body["messages"] == [
            {
                "text": "hello",
                "event_id": "legacy-e1",
                "session_id": "legacy",
                "seq": 1,
            }
        ]
    finally:
        server.shutdown()


def test_messages_endpoint_defaults_after_seq_to_zero(tmp_path):
    collector = _make_collector(tmp_path)
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        port = server.server_address[1]
        _ingest_events(port, [_event(1, "a"), _event(2, "b")])
        with _authed_get(
            f"http://127.0.0.1:{port}/harness/sessions/harness-s2/messages"
        ) as response:
            body = json.loads(response.read())
        assert [m["text"] for m in body["messages"]] == ["a", "b"]
        assert body["max_seq"] == 2
    finally:
        server.shutdown()


def test_messages_endpoint_legacy_after_seq_request_remains_unpaginated(tmp_path):
    collector = _make_collector(tmp_path)
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        port = server.server_address[1]
        _ingest_events(port, [_event(seq, str(seq)) for seq in range(1, 8)])

        with _authed_get(
            f"http://127.0.0.1:{port}/harness/sessions/harness-s2/messages"
            "?after_seq=0"
        ) as response:
            body = json.loads(response.read())

        assert [message["seq"] for message in body["messages"]] == list(range(1, 8))
        assert body["max_seq"] == 7
        assert "has_newer" not in body
    finally:
        server.shutdown()


def test_messages_endpoint_pages_newest_older_and_fixed_forward(tmp_path):
    collector = _make_collector(tmp_path)
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        port = server.server_address[1]
        base = f"http://127.0.0.1:{port}/harness/sessions/harness-s2/messages"
        _ingest_events(port, [_event(seq, str(seq)) for seq in range(1, 8)])

        with _authed_get(f"{base}?limit=3") as response:
            newest = json.loads(response.read())
        with _authed_get(f"{base}?before_seq=5&limit=2") as response:
            older = json.loads(response.read())
        with _authed_get(f"{base}?after_seq=0&limit=2") as response:
            forward = json.loads(response.read())
        _ingest_events(port, [_event(8, "8")])
        with _authed_get(
            f"{base}?after_seq=2&through_seq={forward['max_seq']}&limit=500"
        ) as response:
            bounded = json.loads(response.read())

        assert [message["seq"] for message in newest["messages"]] == [5, 6, 7]
        assert {
            key: newest[key]
            for key in (
                "page_min_seq",
                "page_max_seq",
                "max_seq",
                "has_older",
                "has_newer",
            )
        } == {
            "page_min_seq": 5,
            "page_max_seq": 7,
            "max_seq": 7,
            "has_older": True,
            "has_newer": False,
        }
        assert [message["seq"] for message in older["messages"]] == [3, 4]
        assert older["has_older"] is True
        assert older["has_newer"] is True
        assert [message["seq"] for message in forward["messages"]] == [1, 2]
        assert forward["has_newer"] is True
        assert [message["seq"] for message in bounded["messages"]] == [3, 4, 5, 6, 7]
        assert bounded["max_seq"] == 7
        assert bounded["has_newer"] is False
    finally:
        server.shutdown()


@pytest.mark.parametrize(
    ("query", "detail"),
    [
        ("after_seq=0&before_seq=2&limit=1", "mutually exclusive"),
        ("through_seq=2&limit=1", "through_seq requires after_seq"),
        (
            "after_seq=10&through_seq=5&limit=200",
            "through_seq must not precede after_seq",
        ),
        ("limit=0", "limit must be between 1 and 500"),
        ("limit=501", "limit must be between 1 and 500"),
        ("before_seq=-1&limit=1", "before_seq must be nonnegative"),
        ("after_seq=0&after_seq=1&limit=1", "after_seq must appear once"),
    ],
)
def test_messages_endpoint_rejects_invalid_page_queries(tmp_path, query, detail):
    collector = _make_collector(tmp_path)
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        port = server.server_address[1]
        with pytest.raises(urllib.error.HTTPError) as exc:
            _authed_get(
                f"http://127.0.0.1:{port}/harness/sessions/harness-s2/messages?{query}"
            )
        assert exc.value.code == 400
        assert detail in exc.value.read().decode()
    finally:
        server.shutdown()


def test_messages_endpoint_selectively_gzips_large_pages(tmp_path):
    collector = _make_collector(tmp_path)
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        port = server.server_address[1]
        base = f"http://127.0.0.1:{port}/harness/sessions/harness-s2/messages"
        _ingest_events(port, [_event(1, "compressible transcript " * 100)])

        with _authed_get(f"{base}?limit=1") as response:
            identity = response.read()
        with _authed_get(
            f"{base}?limit=1", headers={"Accept-Encoding": "br, gzip"}
        ) as response:
            compressed = response.read()
            assert response.headers["Content-Encoding"] == "gzip"
            assert response.headers["Vary"] == "Accept-Encoding"

        assert gzip.decompress(compressed) == identity
        assert len(compressed) < len(identity)
    finally:
        server.shutdown()


def test_messages_endpoint_keeps_small_page_uncompressed(tmp_path):
    collector = _make_collector(tmp_path)
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        port = server.server_address[1]
        _ingest_events(port, [_event(1, "small")])
        with _authed_get(
            f"http://127.0.0.1:{port}/harness/sessions/harness-s2/messages?limit=1",
            headers={"Accept-Encoding": "gzip"},
        ) as response:
            body = json.loads(response.read())
            assert response.headers.get("Content-Encoding") is None

        assert body["messages"][0]["text"] == "small"
    finally:
        server.shutdown()


@pytest.mark.parametrize("raw_after_seq", ["nope", "--5"])
def test_messages_endpoint_rejects_non_integer_after_seq(tmp_path, raw_after_seq):
    collector = _make_collector(tmp_path)
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        port = server.server_address[1]
        with pytest.raises(urllib.error.HTTPError) as exc:
            _authed_get(
                f"http://127.0.0.1:{port}/harness/sessions/harness-s2/messages"
                f"?after_seq={raw_after_seq}"
            )
        assert exc.value.code == 400
        assert "after_seq must be an integer" in exc.value.read().decode()
    finally:
        server.shutdown()


def test_messages_endpoint_requires_auth(tmp_path):
    collector = _make_collector(tmp_path)
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        port = server.server_address[1]
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/harness/sessions/harness-s2/messages",
                timeout=5,
            )
        assert exc.value.code == 401
    finally:
        server.shutdown()


def test_session_stream_ws_delivers_new_events(tmp_path):
    collector = _make_collector(tmp_path)
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        port = server.server_address[1]
        _ingest_events(port, [_event(1, "a")])
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            client_handshake(
                sock,
                host=f"127.0.0.1:{port}",
                path="/harness/sessions/harness-s2/stream",
                headers=_AUTH_HEADERS,
            )
            sock.settimeout(5)
            first = json.loads(recv_frame(sock).payload.decode("utf-8"))
            assert first["text"] == "a"
            _ingest_events(port, [_event(2, "b")])
            second = json.loads(recv_frame(sock).payload.decode("utf-8"))
            assert second["text"] == "b"
        finally:
            sock.close()
        # server must still be healthy after an abrupt client disconnect
        with _authed_get(f"http://127.0.0.1:{port}/harness/hosts") as response:
            assert response.status == 200
    finally:
        server.shutdown()


def test_session_stream_ws_initial_catch_up_overlays_canonical_event_metadata(
    tmp_path,
):
    collector = _make_collector(tmp_path)
    HarnessRegistry(collector.duckdb_path).append_event(
        event_id="legacy-e1",
        session_id="legacy",
        seq=1,
        event_type="assistant_output",
        payload={"text": "hello"},
    )
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        port = server.server_address[1]
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            client_handshake(
                sock,
                host=f"127.0.0.1:{port}",
                path="/harness/sessions/legacy/stream",
                headers=_AUTH_HEADERS,
            )
            sock.settimeout(5)
            message = json.loads(recv_frame(sock).payload.decode("utf-8"))
            assert message == {
                "text": "hello",
                "event_id": "legacy-e1",
                "session_id": "legacy",
                "seq": 1,
            }
        finally:
            sock.close()
    finally:
        server.shutdown()


def test_session_stream_ws_answers_ping_and_stays_live(tmp_path):
    collector = _make_collector(tmp_path)
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        port = server.server_address[1]
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            client_handshake(
                sock,
                host=f"127.0.0.1:{port}",
                path="/harness/sessions/harness-s2/stream",
                headers=_AUTH_HEADERS,
            )
            sock.settimeout(2)
            client_send_frame(sock, OPCODE_PING, b"hb")
            pong = recv_frame(sock)
            assert pong.opcode == OPCODE_PONG
            assert pong.payload == b"hb"

            _ingest_events(port, [_event(1, "after-ping")])
            message = json.loads(recv_frame(sock).payload.decode("utf-8"))
            assert message["text"] == "after-ping"
        finally:
            sock.close()
    finally:
        server.shutdown()


def test_session_stream_ws_respects_after_seq(tmp_path):
    collector = _make_collector(tmp_path)
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        port = server.server_address[1]
        _ingest_events(port, [_event(1, "old"), _event(2, "new")])
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            client_handshake(
                sock,
                host=f"127.0.0.1:{port}",
                path="/harness/sessions/harness-s2/stream?after_seq=1",
                headers=_AUTH_HEADERS,
            )
            sock.settimeout(5)
            message = json.loads(recv_frame(sock).payload.decode("utf-8"))
            assert message["text"] == "new"

            _ingest_events(port, [_event(3, "later")])
            message = json.loads(recv_frame(sock).payload.decode("utf-8"))
            assert message["text"] == "later"
        finally:
            sock.close()
    finally:
        server.shutdown()


def test_session_stream_ws_requires_auth(tmp_path):
    collector = _make_collector(tmp_path)
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        port = server.server_address[1]
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            with pytest.raises(RuntimeError) as exc:
                client_handshake(
                    sock,
                    host=f"127.0.0.1:{port}",
                    path="/harness/sessions/harness-s2/stream",
                )
            assert "401" in str(exc.value)
        finally:
            sock.close()
    finally:
        server.shutdown()


def test_session_stream_ws_rejects_missing_websocket_key(tmp_path):
    collector = _make_collector(tmp_path)
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        port = server.server_address[1]
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            request = (
                "GET /harness/sessions/harness-s2/stream HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                f"Authorization: Bearer {_TEST_TOKEN}\r\n"
                "\r\n"
            )
            sock.sendall(request.encode("ascii"))
            sock.settimeout(5)
            response = b""
            while b"\r\n\r\n" not in response:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk
            status_line = response.decode("iso-8859-1").split("\r\n", 1)[0]
            assert " 400 " in status_line
        finally:
            sock.close()
    finally:
        server.shutdown()


def test_session_stream_ws_upgrade_is_http_1_1(tmp_path):
    """Strict WebSocket clients (URLSessionWebSocketTask) reject HTTP/1.0 101."""
    collector = _make_collector(tmp_path)
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        port = server.server_address[1]
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            request = (
                "GET /harness/sessions/harness-s2/stream HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                f"Authorization: Bearer {_TEST_TOKEN}\r\n"
                "\r\n"
            )
            sock.sendall(request.encode("ascii"))
            sock.settimeout(5)
            status_line = b""
            while not status_line.endswith(b"\r\n"):
                status_line += sock.recv(1)
            assert status_line.startswith(b"HTTP/1.1 101"), status_line
        finally:
            sock.close()
    finally:
        server.shutdown()


def test_sync_created_harness_session_preserves_mode(tmp_path):
    collector = _make_collector(tmp_path)
    collector._sync_created_harness_session(
        "mac-mini",
        {"harness": "agy", "mode": "structured", "prompt": "x"},
        json.dumps(
            {
                "session_id": "harness-sync-mode",
                "mode": "structured",
                "harness": "agy",
                "status": "running",
            }
        ),
    )
    registry = HarnessRegistry(collector.duckdb_path)
    session = registry.get_session("harness-sync-mode")
    assert session is not None
    assert session.mode == "structured"


def test_sync_created_harness_session_preserves_permission_mode(tmp_path):
    collector = _make_collector(tmp_path)
    collector._sync_created_harness_session(
        "mac-mini",
        {"harness": "claude-code", "permission_mode": "auto", "cwd": "/tmp/nexus"},
        json.dumps(
            {
                "session_id": "harness-sync-permission",
                "mode": "structured",
                "harness": "claude-code",
                "status": "running",
            }
        ),
    )
    registry = HarnessRegistry(collector.duckdb_path)
    session = registry.get_session("harness-sync-permission")
    assert session is not None
    assert session.permission_mode == "auto"


def test_sync_created_harness_session_preserves_run_preferences(tmp_path):
    collector = _make_collector(tmp_path)
    collector._sync_created_harness_session(
        "mac-mini",
        {
            "harness": "claude-code",
            "mode": "structured",
            "model": "claude-fable-5[1m]",
            "thinking_effort": "xhigh",
        },
        json.dumps(
            {
                "session_id": "harness-sync-preferences",
                "mode": "structured",
                "harness": "claude-code",
                "status": "running",
            }
        ),
    )
    registry = HarnessRegistry(collector.duckdb_path)
    session = registry.get_session("harness-sync-preferences")
    assert session is not None
    assert session.model == "claude-fable-5[1m]"
    assert session.thinking_effort == "xhigh"


def test_proxy_forwards_session_turn_to_harnessd(tmp_path):
    _FakeHarnessHandler.requests = []
    harness_server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeHarnessHandler)
    harness_thread = metrics.threading.Thread(
        target=harness_server.serve_forever, daemon=True
    )
    harness_thread.start()
    harness_port = harness_server.server_address[1]

    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    registry = HarnessRegistry(duckdb_path)
    registry.register_host(
        host_id="mac-mini",
        display_name="Mac Mini",
        kind="macos",
        local_url=f"http://127.0.0.1:{harness_port}",
        capabilities={"harnesses": [{"name": "agy", "enabled": True}]},
    )
    registry.create_session(
        session_id="harness-running",
        host_id="mac-mini",
        harness="agy",
        command="agy",
        mode="structured",
        status="running",
    )
    collector = MetricsCollector(
        duckdb_path=duckdb_path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        ttl_seconds=60,
        api_token="host-secret",
    )
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        port = server.server_address[1]
        # turns: forwards body, returns turn_id
        turn = Request(
            f"http://127.0.0.1:{port}/harness/sessions/harness-running/turns",
            data=json.dumps(
                {
                    "text": "second turn",
                    "model": "gemini-3.6-flash-high",
                    "thinking_effort": "high",
                }
            ).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", **_AUTH_HEADERS},
        )
        with urlopen(turn, timeout=3) as res:
            assert json.loads(res.read().decode("utf-8"))["turn_id"] == "turn-xyz"
        # permission: forwards body
        perm = Request(
            f"http://127.0.0.1:{port}/harness/sessions/harness-running/permission",
            data=json.dumps({"request_id": "r1", "decision": "allow"}).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", **_AUTH_HEADERS},
        )
        with urlopen(perm, timeout=3) as res:
            assert json.loads(res.read().decode("utf-8"))["ok"] is True
        # interrupt: no body required
        interrupt = Request(
            f"http://127.0.0.1:{port}/harness/sessions/harness-running/interrupt",
            data=b"",
            method="POST",
            headers=dict(_AUTH_HEADERS),
        )
        with urlopen(interrupt, timeout=3) as res:
            assert json.loads(res.read().decode("utf-8"))["ok"] is True
    finally:
        server.shutdown()
        server.server_close()
        harness_server.shutdown()
        harness_server.server_close()

    forwarded = {r["path"]: r for r in _FakeHarnessHandler.requests}
    assert "/sessions/harness-running/turns" in forwarded
    assert forwarded["/sessions/harness-running/turns"]["body"] == {
        "text": "second turn",
        "model": "gemini-3.6-flash-high",
        "thinking_effort": "high",
    }
    assert forwarded["/sessions/harness-running/turns"]["authorization"] == (
        "Bearer host-secret"
    )
    session = HarnessRegistry(duckdb_path).get_session("harness-running")
    assert session is not None
    assert session.model == "gemini-3.6-flash-high"
    assert session.thinking_effort == "high"
    assert "/sessions/harness-running/permission" in forwarded
    assert forwarded["/sessions/harness-running/permission"]["body"] == {
        "request_id": "r1",
        "decision": "allow",
    }
    assert "/sessions/harness-running/interrupt" in forwarded


def _recovery_collector(tmp_path, *, native_session_id="provider-thread-1"):
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    registry = HarnessRegistry(duckdb_path)
    registry.register_host(
        host_id="mac-mini",
        display_name="Mac Mini",
        kind="macos",
        local_url="http://127.0.0.1:1",
    )
    registry.create_session(
        session_id="harness-recover",
        host_id="mac-mini",
        harness="codex",
        command="codex",
        mode="structured",
        status="running",
        cwd="/tmp/recovery-worktree",
        native_session_id=native_session_id,
    )
    return MetricsCollector(
        duckdb_path=duckdb_path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
    )


def test_turn_recovery_retries_original_payload_once(monkeypatch, tmp_path):
    collector = _recovery_collector(tmp_path)
    responses = iter(
        [
            (404, '{"error":"unknown structured session: harness-recover"}\n'),
            (
                200,
                '{"session_id":"harness-recover","status":"running",'
                '"recovered":true,"native_session_id":"provider-thread-1"}\n',
            ),
            (202, '{"turn_id":"turn-recovered"}\n'),
        ]
    )
    calls: list[tuple[str, str, dict]] = []

    def request(_host, path, *, method, payload, **_kwargs):
        calls.append((method, path, dict(payload)))
        return next(responses)

    monkeypatch.setattr(collector, "_harness_request", request)
    turn = {"text": "continue safely", "model": "gpt-5.6"}

    status, body = collector.proxy_harness_session_action(
        "harness-recover", "turns", turn
    )

    assert status == 202
    assert json.loads(body)["turn_id"] == "turn-recovered"
    assert calls == [
        ("POST", "/sessions/harness-recover/turns", turn),
        (
            "POST",
            "/sessions/harness-recover/recover",
            {"native_session_id": "provider-thread-1"},
        ),
        ("POST", "/sessions/harness-recover/turns", turn),
    ]


def test_central_terminate_waits_for_recovery_retry(monkeypatch, tmp_path):
    collector = _recovery_collector(tmp_path)
    recovery_reached = threading.Event()
    allow_recovery = threading.Event()
    terminate_started = threading.Event()
    terminate_reached = threading.Event()
    turn_calls = 0

    def request(_host, path, **_kwargs):
        nonlocal turn_calls
        if path.endswith("/turns"):
            turn_calls += 1
            if turn_calls == 1:
                return 404, '{"error":"unknown structured session: harness-recover"}\n'
            return 202, '{"turn_id":"turn-recovered"}\n'
        if path.endswith("/recover"):
            recovery_reached.set()
            assert allow_recovery.wait(timeout=5)
            return 200, '{"status":"running","recovered":true}\n'
        if path.endswith("/terminate"):
            terminate_reached.set()
            return 200, '{"session_id":"harness-recover","status":"terminated"}\n'
        raise AssertionError(path)

    monkeypatch.setattr(collector, "_harness_request", request)
    results: dict[str, tuple[int, str]] = {}
    recovery = threading.Thread(
        target=lambda: results.setdefault(
            "recovery",
            collector.proxy_harness_session_action(
                "harness-recover", "turns", {"text": "x"}
            ),
        )
    )

    def terminate():
        terminate_started.set()
        results["terminate"] = collector.proxy_terminate_harness_session(
            "harness-recover"
        )

    termination = threading.Thread(target=terminate)
    recovery.start()
    assert recovery_reached.wait(timeout=5)
    termination.start()
    assert terminate_started.wait(timeout=5)
    assert not terminate_reached.wait(timeout=0.1)
    allow_recovery.set()
    recovery.join(timeout=5)
    termination.join(timeout=5)

    assert results["recovery"][0] == 202
    assert results["terminate"][0] == 200
    assert (
        HarnessRegistry(collector.duckdb_path).get_session("harness-recover").status
        == "terminated"
    )


@pytest.mark.parametrize(
    ("action", "status", "body"),
    [
        ("turns", 404, '{"error":"some other missing resource"}\n'),
        ("turns", 409, '{"error":"turn already in flight"}\n'),
        ("turns", 500, '{"error":"driver failed"}\n'),
        ("turns", 502, '{"error":"host unreachable"}\n'),
        ("permission", 404, '{"error":"unknown structured session"}\n'),
        ("interrupt", 404, '{"error":"unknown structured session"}\n'),
    ],
)
def test_session_action_does_not_recover_unqualified_failure(
    monkeypatch, tmp_path, action, status, body
):
    collector = _recovery_collector(tmp_path)
    calls: list[str] = []

    def request(_host, path, **_kwargs):
        calls.append(path)
        return status, body

    monkeypatch.setattr(collector, "_harness_request", request)

    actual = collector.proxy_harness_session_action(
        "harness-recover", action, {"text": "x"}
    )

    assert actual == (status, body)
    assert calls == [f"/sessions/harness-recover/{action}"]


def test_turn_recovery_without_native_id_returns_actionable_conflict(
    monkeypatch, tmp_path
):
    collector = _recovery_collector(tmp_path, native_session_id=None)
    monkeypatch.setattr(
        collector,
        "_harness_request",
        lambda *_args, **_kwargs: (
            404,
            '{"error":"unknown structured session: harness-recover"}\n',
        ),
    )

    status, body = collector.proxy_harness_session_action(
        "harness-recover", "turns", {"text": "x"}
    )

    assert status == 409
    assert json.loads(body)["error"] == (
        "Session cannot be resumed after the harness restart. "
        "Continue it in a new session."
    )


@pytest.mark.parametrize("recovery_status", [401, 403, 404, 500, 502])
def test_turn_recovery_preserves_transient_or_unexpected_recovery_failure(
    monkeypatch, tmp_path, recovery_status
):
    collector = _recovery_collector(tmp_path)
    responses = iter(
        [
            (404, '{"error":"unknown structured session: harness-recover"}\n'),
            (recovery_status, '{"error":"recovery service unavailable"}\n'),
        ]
    )
    monkeypatch.setattr(
        collector, "_harness_request", lambda *_args, **_kwargs: next(responses)
    )

    status, body = collector.proxy_harness_session_action(
        "harness-recover", "turns", {"text": "x"}
    )

    assert status == recovery_status
    assert json.loads(body)["error"] == "recovery service unavailable"


def test_recovery_native_id_falls_back_to_structured_event_payload(tmp_path):
    collector = _recovery_collector(tmp_path, native_session_id=None)
    registry = HarnessRegistry(collector.duckdb_path)
    registry.append_event(
        session_id="harness-recover",
        event_type="status",
        payload={"payload": {"native_session_id": "event-thread-1"}},
    )

    assert (
        collector._native_session_id_for_recovery("harness-recover") == "event-thread-1"
    )


def test_session_snapshot_has_no_transcript_chunks_key(tmp_path):
    """Scrollback comes from terminal.output events; the chunk table is gone."""
    collector = _make_collector(tmp_path)
    registry = HarnessRegistry(collector.duckdb_path)
    session = registry.create_session(
        host_id="h1", harness="shell", command="sh", mode="pty"
    )
    registry.append_event(
        session_id=session.session_id,
        event_type="terminal.output",
        payload={"text": "hello"},
    )

    snapshot = collector.harness_session_snapshot(session.session_id)

    assert "transcript_chunks" not in snapshot
    assert any(e["event_type"] == "terminal.output" for e in snapshot["events"])


def test_prometheus_exports_dropped_harness_events(tmp_path):
    """A non-zero counter means transcript content was permanently lost."""
    from drover.server.harness import daemon as daemon_mod

    daemon_mod.reset_dropped_event_count()
    daemon_mod.record_dropped_events(4)

    collector = _make_collector(tmp_path)
    text = collector.render_prometheus()

    assert "drover_harness_dropped_events_total 4" in text


def test_prometheus_exports_undelivered_harness_events(tmp_path):
    """Loss in transit to the hub (#99) is a different loss from a failed
    local write, and the dropped-events counter never saw it."""
    from drover.server.harness import daemon as daemon_mod

    daemon_mod.reset_undelivered_event_count()
    daemon_mod.record_undelivered_events(7)

    collector = _make_collector(tmp_path)
    text = collector.render_prometheus()

    assert "drover_harness_undelivered_events_total 7" in text
    daemon_mod.reset_undelivered_event_count()


def test_harness_snapshot_does_not_copy_the_database(tmp_path, monkeypatch):
    """The hub DB is ~483MB; copying it per poll was a 16% disk duty cycle."""
    collector = _make_collector(tmp_path)
    registry = HarnessRegistry(collector.duckdb_path)
    registry.create_session(host_id="h1", harness="shell", command="sh")

    copies: list = []
    real_copy = shutil.copy2

    def spy(*args, **kwargs):
        copies.append(args)
        return real_copy(*args, **kwargs)

    monkeypatch.setattr(shutil, "copy2", spy)

    snapshot = collector.harness_snapshot()

    assert copies == [], "harness_snapshot must query the live DB, not copy it"
    assert len(snapshot["sessions"]) == 1


def test_render_harness_json_caches_within_ttl(tmp_path, monkeypatch):
    collector = _make_collector(tmp_path)
    registry = HarnessRegistry(collector.duckdb_path)
    registry.create_session(host_id="h1", harness="shell", command="sh")

    calls = {"n": 0}
    real = collector.harness_snapshot

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(collector, "harness_snapshot", counting)

    first = collector.render_harness_json()
    second = collector.render_harness_json()

    assert calls["n"] == 1, "second call inside the TTL must be served from cache"
    assert first == second


def test_render_harness_json_refreshes_after_ttl(tmp_path, monkeypatch):
    collector = _make_collector(tmp_path)
    registry = HarnessRegistry(collector.duckdb_path)
    registry.create_session(host_id="h1", harness="shell", command="sh")
    collector.harness_ttl_seconds = 0.0

    calls = {"n": 0}
    real = collector.harness_snapshot

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(collector, "harness_snapshot", counting)
    collector.render_harness_json()
    collector.render_harness_json()

    assert calls["n"] == 2


def test_quality_and_observatory_snapshots_read_an_isolated_copy(tmp_path, monkeypatch):
    """The slow snapshots must NOT touch the live database.

    quality_snapshot takes ~20s on a real store (measured 19.4s at 686MB).
    Run against the live file it shares one DuckDB instance with the harness
    registry, saturates the scheduler, spills to temp storage, and starves
    every other DB-backed endpoint -- /harness timed out for minutes at a
    time while /healthz stayed instant. The copy is isolation, not caching:
    a separate file is a separate DuckDB instance, which is the whole point.

    This asserts the paths handed to the snapshot functions are NOT the live
    database. An earlier version of this test asserted the opposite and is
    what let the regression through.
    """
    collector = _make_collector(tmp_path)
    live = Path(collector.duckdb_path).resolve()
    seen: list[Path] = []

    def fake_quality(*, duckdb_path, **kwargs):
        seen.append(Path(duckdb_path).resolve())
        return {"runtime_audit": {}}

    def fake_observatory(*, duckdb_path, **kwargs):
        seen.append(Path(duckdb_path).resolve())
        return {}

    monkeypatch.setattr(metrics, "quality_snapshot", fake_quality)
    monkeypatch.setattr(metrics, "pipeline_observatory_snapshot", fake_observatory)

    quality = collector._quality_snapshot()
    collector._observatory_snapshot(quality)

    assert len(seen) == 2, "both snapshots should have run"
    for path in seen:
        assert path != live, f"slow snapshot read the live DB at {path}"
        assert path.name == live.name, "copy should keep the database filename"


def test_copy_backed_snapshots_use_the_parallel_snapshot_role(tmp_path, monkeypatch):
    """The isolated copy is the only reader allowed extra DuckDB threads.

    ``threads`` is instance-wide, so it can only be raised safely on a
    connection that owns its instance. These two do -- they read a private
    tempdir copy -- and at threads=1 they were the whole /metrics bill (#78).
    """
    collector = _make_collector(tmp_path)
    roles: list[str] = []

    def fake_quality(*, duckdb_path, role="diagnostic", **kwargs):
        roles.append(role)
        return {"runtime_audit": {}}

    def fake_observatory(*, duckdb_path, role="diagnostic", **kwargs):
        roles.append(role)
        return {}

    monkeypatch.setattr(metrics, "quality_snapshot", fake_quality)
    monkeypatch.setattr(metrics, "pipeline_observatory_snapshot", fake_observatory)

    quality = collector._quality_snapshot()
    collector._observatory_snapshot(quality)

    assert roles == ["snapshot", "snapshot"]


def test_live_database_metrics_stay_on_the_single_threaded_role(tmp_path, monkeypatch):
    """Everything in the same refresh that reads the *live* file keeps
    threads=1. Those connections share the hub's DuckDB instance, and that is
    the sharing that caused the 2026-08-04 outage (#91)."""
    collector = _make_collector(tmp_path)
    roles: list[str] = []
    real_open = metrics.open_duckdb_connection

    def _recording_open(*args, **kwargs):
        roles.append(str(kwargs.get("role", "worker")))
        return real_open(*args, **kwargs)

    monkeypatch.setattr(metrics, "open_duckdb_connection", _recording_open)
    metrics._append_operational_health_metrics(
        [], Path(collector.duckdb_path), {"categories": {}}
    )

    assert roles, "expected at least one live-database read"
    assert set(roles) == {"diagnostic"}


class _CountingQuality:
    """Wraps the real quality snapshot so renders stay realistic but countable.

    Each render is stamped with its ordinal in ``score`` so a caller can tell
    which one it was handed, and the second render onward can be held open to
    observe what a scrape gets while a rebuild is in flight.
    """

    def __init__(self, monkeypatch, *, hold_from: int | None = None):
        self._real = metrics.quality_snapshot
        self.threads: list[str] = []
        self.started = threading.Semaphore(0)
        self.release = threading.Event()
        self._hold_from = hold_from
        monkeypatch.setattr(metrics, "quality_snapshot", self)

    def __call__(self, **kwargs):
        ordinal = len(self.threads) + 1
        self.threads.append(threading.current_thread().name)
        self.started.release()
        if self._hold_from is not None and ordinal >= self._hold_from:
            assert self.release.wait(30), "held render was never released"
        snapshot = self._real(**kwargs)
        snapshot["score"] = float(ordinal)
        return snapshot

    def wait_for_render(self, ordinal: int) -> None:
        assert self.started.acquire(timeout=30), f"render {ordinal} never started"


def test_warm_takes_the_cold_render_off_the_request_path(tmp_path, monkeypatch):
    """Nothing warmed the metrics cache after a restart, so the first scrape
    paid for the cold DuckDB open and the whole parquet glob: 35.6s measured
    on the Mac hub (#78). The server should pay that itself, as it already
    does for the cockpit.
    """
    collector = _make_collector(tmp_path)
    quality = _CountingQuality(monkeypatch)

    collector.warm()
    assert len(quality.threads) == 1, "warm should build the render"

    collector.render_prometheus()
    assert (
        len(quality.threads) == 1
    ), "the first scrape after warm should be a cache hit"


def test_expired_metrics_cache_is_served_stale_while_it_refreshes(
    tmp_path, monkeypatch
):
    """An expired cache must not make a scraper wait for the rebuild.

    The rebuild is seconds of DuckDB work behind a 60s TTL, so under
    Prometheus's scrape interval roughly one scrape in four used to absorb all
    of it. It is served stale and refreshed behind the request instead.
    """
    collector = _make_collector(tmp_path)
    quality = _CountingQuality(monkeypatch, hold_from=2)

    assert json.loads(collector.render_json())["quality"]["score"] == 1.0
    quality.wait_for_render(1)
    collector._cached_until = monotonic()  # expire it

    started = monotonic()
    stale = json.loads(collector.render_json())
    elapsed = monotonic() - started

    assert stale["quality"]["score"] == 1.0, "should serve the previous render"
    assert elapsed < 2.0, f"stale read blocked for {elapsed:.1f}s"

    quality.wait_for_render(2)
    assert (
        quality.threads[1] != quality.threads[0]
    ), "the rebuild must not run on the calling thread"

    quality.release.set()
    deadline = monotonic() + 30
    while monotonic() < deadline:
        if json.loads(collector.render_json())["quality"]["score"] == 2.0:
            break
    else:  # pragma: no cover - only on a hung refresh
        pytest.fail("background refresh never replaced the stale render")


def test_expired_cache_triggers_only_one_background_refresh(tmp_path, monkeypatch):
    """Concurrent scrapes share one rebuild; they must not each start one."""
    collector = _make_collector(tmp_path)
    quality = _CountingQuality(monkeypatch, hold_from=2)

    collector.render_json()
    quality.wait_for_render(1)
    collector._cached_until = monotonic()

    for _ in range(5):
        collector.render_json()

    quality.wait_for_render(2)
    quality.release.set()
    assert (
        len(quality.threads) == 2
    ), f"expected one background rebuild, got {len(quality.threads) - 1}"


def test_cache_older_than_the_stale_window_is_rebuilt_synchronously(
    tmp_path, monkeypatch
):
    """Serving stale forever would freeze the numbers if refreshes kept
    failing, and frozen metrics look healthy. Past the window the caller
    waits for a real rebuild again."""
    collector = _make_collector(tmp_path)
    collector.max_stale_seconds = 5.0
    quality = _CountingQuality(monkeypatch)

    collector.render_json()
    collector._cached_until = monotonic() - 10.0

    payload = json.loads(collector.render_json())

    assert quality.threads == [threading.current_thread().name] * 2
    assert payload["quality"]["score"] == 2.0


def test_harness_snapshot_works_while_this_process_holds_the_db(tmp_path):
    """The hub serves snapshots from the same process that owns the database.

    DuckDB's single-writer lock is cross-process: a second *process* opening
    the file fails, but the owning process can open it again. harness_snapshot
    reads the live file (no copy), so it depends on that. If snapshot rendering
    ever moves to a subprocess or a separate worker, this breaks and the fleet
    silently renders empty -- the error path returns hosts=[] sessions=[].
    """
    collector = _make_collector(tmp_path)
    registry = HarnessRegistry(collector.duckdb_path)
    registry.create_session(host_id="h1", harness="shell", command="sh")

    # Mimic drover-server: hold a long-lived connection for the whole render.
    held = duckdb.connect(str(collector.duckdb_path))
    try:
        held.execute("SELECT 1").fetchall()
        snapshot = collector.harness_snapshot()
    finally:
        held.close()

    assert "error" not in snapshot, snapshot.get("error")
    assert len(snapshot["sessions"]) == 1


def _seed_sessions(duckdb_path, *, archived: int, live: int) -> None:
    """Register a host and a mix of finished and live sessions."""
    registry = HarnessRegistry(duckdb_path)
    registry.register_host(host_id="h1", display_name="H1", kind="macos")
    for i in range(archived + live):
        s = registry.create_session(host_id="h1", harness="claude-code", command="x")
        status = "terminated" if i < archived else "running"
        registry.update_session_status(s.session_id, status)


def test_fleet_render_caps_archived_sessions_by_default(tmp_path):
    """115 of 120 sessions were terminated and every one shipped on each poll."""
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    _seed_sessions(duckdb_path, archived=40, live=2)
    collector = MetricsCollector(
        duckdb_path=duckdb_path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        ttl_seconds=60,
    )

    sessions = json.loads(collector.render_harness_json())["sessions"]
    statuses = [s["status"] for s in sessions]

    assert statuses.count("terminated") == collector.archived_session_limit == 20
    assert statuses.count("running") == 2, "a live session was capped away"


def test_a_larger_archived_request_is_not_served_the_cached_default(tmp_path):
    """The fleet render is cached for 2s; the cap must be part of that identity.

    Without this the first default poll populates the cache and a client
    asking for 100 gets handed 20 back, which looks like the cap ignoring it.
    """
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    _seed_sessions(duckdb_path, archived=40, live=1)
    collector = MetricsCollector(
        duckdb_path=duckdb_path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        ttl_seconds=60,
    )

    default = json.loads(collector.render_harness_json())["sessions"]
    larger = json.loads(collector.render_harness_json(archived_limit=40))["sessions"]

    assert len([s for s in default if s["status"] == "terminated"]) == 20
    assert len([s for s in larger if s["status"] == "terminated"]) == 40


def test_archived_query_param_is_clamped_not_rejected(tmp_path):
    """A stray query string must not fail a whole fleet render on the poll path."""
    from drover.server.metrics import MAX_ARCHIVED_SESSION_LIMIT
    from drover.server.web.app import _archived_limit_kwargs

    assert _archived_limit_kwargs({}) == {}
    assert _archived_limit_kwargs({"archived": ["oops"]}) == {}
    assert _archived_limit_kwargs({"archived": ["5"]}) == {"archived_limit": 5}
    assert _archived_limit_kwargs({"archived": ["-3"]}) == {"archived_limit": 0}
    assert _archived_limit_kwargs({"archived": ["9999"]}) == {
        "archived_limit": MAX_ARCHIVED_SESSION_LIMIT
    }
