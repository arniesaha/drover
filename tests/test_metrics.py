from __future__ import annotations

from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import base64
import os
import socket
import threading
from time import monotonic
import urllib.error
import urllib.request
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from drover.schema import bootstrap
from drover.server import metrics
from drover.server.harness.daemon import (
    DEFAULT_PRESETS,
    HarnessDaemonState,
    create_harness_server,
    register_daemon_host,
)
from drover.server.harness.pty import PtySessionManager
from drover.server.harness.registry import HarnessRegistry
from drover.server.harness.websocket import (
    client_handshake,
    client_send_json,
    recv_frame,
)
from drover.server.metrics import MetricsCollector, start_metrics_server
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
            "local_ready": True,
            "allows_anthropic": True,
            "allows_local": True,
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
    assert 'drover_redis_job_stream_length{queue="summarize"} 4' in text
    assert 'drover_redis_job_stream_pending{queue="summarize"} 1' in text
    assert (
        'drover_agent_adoption_ready{runtime="openclaw-main",status="active"} 1' in text
    )
    assert 'drover_agent_adoption_observed_events{runtime="openclaw-main"} 12' in text


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
    registry.append_transcript_chunk(
        session_id=session.session_id,
        content_redacted="hello from transcript\n",
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


def test_harness_request_prefers_live_relay(collector_with_hosts) -> None:
    collector = collector_with_hosts
    fake = _FakeRelay()
    collector.relay_manager = fake
    host = collector._harness_host("laptop")
    status, body = collector._harness_request(host, "/sessions", method="GET")
    assert status == 200
    assert fake.calls == [("laptop", "GET", "/sessions", {})]


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
    registry.append_transcript_chunk(
        session_id=source.session_id,
        content_redacted="We just implemented central host heartbeats.\n",
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
                    "type": "user_input",
                    "payload": {},
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
        {"harness": "gemini", "mode": "structured", "prompt": "x"},
        json.dumps(
            {
                "session_id": "harness-sync-mode",
                "mode": "structured",
                "harness": "gemini",
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
        capabilities={"harnesses": [{"name": "gemini", "enabled": True}]},
    )
    registry.create_session(
        session_id="harness-running",
        host_id="mac-mini",
        harness="gemini",
        command="gemini",
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
            data=json.dumps({"text": "second turn"}).encode("utf-8"),
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
        "text": "second turn"
    }
    assert forwarded["/sessions/harness-running/turns"]["authorization"] == (
        "Bearer host-secret"
    )
    assert "/sessions/harness-running/permission" in forwarded
    assert forwarded["/sessions/harness-running/permission"]["body"] == {
        "request_id": "r1",
        "decision": "allow",
    }
    assert "/sessions/harness-running/interrupt" in forwarded


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
