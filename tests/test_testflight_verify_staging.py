"""Exercise the staging gate through a real local HTTPS server."""

import hashlib
import importlib.util
import json
import ssl
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts/testflight/verify_staging.py"
SHA = "a" * 40
HOST = "testflight-staging-mac-mini"
TOKEN = "private-preflight-token"


@pytest.fixture
def gate():
    assert SCRIPT.exists(), "HTTPS staging preflight client is missing"
    spec = importlib.util.spec_from_file_location("verify_staging", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def stage(tmp_path, monkeypatch):
    key = tmp_path / "key.pem"
    certificate = tmp_path / "cert.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(certificate),
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost",
        ],
        check=True,
        capture_output=True,
    )
    monkeypatch.setenv("SSL_CERT_FILE", str(certificate))
    host = {
        "host_id": HOST,
        "status": "online",
        "capabilities": {"harnesses": [{"name": "claude-code", "enabled": True}]},
    }
    payloads = {
        "/release-identity": {
            "source_sha": SHA,
            "role": "testflight-staging",
            "package_version": "0.4.13",
            "staging_probe": {
                "source_sha": SHA,
                "host_id": HOST,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "session_id_sha256": "b" * 64,
            },
        },
        "/readyz": {"ready": True},
        "/harness/hosts": {"hosts": [host]},
    }
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append((self.path, self.headers.get("Authorization")))
            value = payloads.get(self.path)
            if value == "redirect":
                self.send_response(302)
                self.send_header("Location", "/private-redirect-target")
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(
                value if isinstance(value, bytes) else json.dumps(value).encode()
            )

    server = ThreadingHTTPServer(("localhost", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://localhost:{server.server_port}", payloads, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def invoke(gate, stage, tmp_path, *, url=None):
    record = tmp_path / "preflight.json"
    result = gate.main(
        [
            "--url",
            url or stage[0],
            "--token",
            TOKEN,
            "--expected-sha",
            SHA,
            "--record",
            str(record),
        ]
    )
    return result, json.loads(record.read_text())


def test_success_fetches_only_three_gets_and_retains_only_safe_fields(
    gate, stage, tmp_path
):
    result, record = invoke(gate, stage, tmp_path, url=stage[0] + "/")
    assert result == 0
    assert record == {
        "source_sha": SHA,
        "package_version": "0.4.13",
        "role": "testflight-staging",
        "probe_completed_at": stage[1]["/release-identity"]["staging_probe"][
            "completed_at"
        ],
        "host_id": HOST,
        "staging_url_sha256": hashlib.sha256(stage[0].encode()).hexdigest(),
    }
    assert stage[2] == [
        (path, f"Bearer {TOKEN}")
        for path in ("/release-identity", "/readyz", "/harness/hosts")
    ]


@pytest.mark.parametrize(
    "runtime,accepted",
    [
        ("claude-code", True),
        ("codex", True),
        ("agy", False),
        ("deepseek-harness", False),
        ("deepseek", False),
    ],
)
def test_only_runtimes_allowed_by_staging_launch_policy_pass(
    gate, stage, tmp_path, runtime, accepted
):
    stage[1]["/harness/hosts"]["hosts"][0]["capabilities"]["harnesses"] = [
        {"name": runtime, "enabled": True}
    ]
    result, record = invoke(gate, stage, tmp_path)
    if accepted:
        assert result == 0
        assert record["host_id"] == HOST
    else:
        assert result == 1
        assert record == {"category": "stage_harness_unavailable"}


@pytest.mark.parametrize(
    "problem,category",
    [
        ("redirect", "stage_request_failed"),
        ("role", "stage_identity_mismatch"),
        ("sha", "stage_identity_mismatch"),
        ("ready", "stage_not_ready"),
        ("unreadable", "stage_response_invalid"),
        ("missing-host", "stage_host_unavailable"),
        ("offline", "stage_host_unavailable"),
        ("capability", "stage_harness_unavailable"),
        ("disabled", "stage_harness_unavailable"),
        ("missing-probe", "stage_probe_invalid"),
        ("probe-sha", "stage_probe_invalid"),
        ("probe-host", "stage_probe_invalid"),
        ("stale", "stage_probe_stale"),
        ("future", "stage_probe_stale"),
        ("naive", "stage_probe_invalid"),
        ("digest", "stage_probe_invalid"),
        ("version", "stage_identity_mismatch"),
    ],
)
def test_gate_fails_closed_without_diagnostics(
    gate, stage, tmp_path, capsys, problem, category
):
    identity = stage[1]["/release-identity"]
    host = stage[1]["/harness/hosts"]["hosts"][0]
    probe = identity["staging_probe"]
    if problem == "redirect":
        stage[1]["/release-identity"] = "redirect"
    elif problem == "role":
        identity["role"] = "production"
    elif problem == "sha":
        identity["source_sha"] = "c" * 40
    elif problem == "ready":
        stage[1]["/readyz"] = {"ready": False}
    elif problem == "unreadable":
        stage[1]["/readyz"] = b"private bad body"
    elif problem == "missing-host":
        stage[1]["/harness/hosts"] = {"hosts": []}
    elif problem == "offline":
        host["status"] = "offline"
    elif problem == "capability":
        host["capabilities"]["harnesses"][0]["name"] = "shell"
    elif problem == "disabled":
        host["capabilities"]["harnesses"][0]["enabled"] = False
    elif problem == "missing-probe":
        identity["staging_probe"] = None
    elif problem == "probe-sha":
        probe["source_sha"] = "c" * 40
    elif problem == "probe-host":
        probe["host_id"] = "production-host"
    elif problem == "stale":
        probe["completed_at"] = (
            datetime.now(timezone.utc) - timedelta(minutes=31)
        ).isoformat()
    elif problem == "future":
        probe["completed_at"] = (
            datetime.now(timezone.utc) + timedelta(minutes=1)
        ).isoformat()
    elif problem == "naive":
        probe["completed_at"] = "2026-09-07T12:00:00"
    elif problem == "digest":
        probe["session_id_sha256"] = "private-session"
    elif problem == "version":
        identity["package_version"] = "private diagnostic body"
    result, record = invoke(gate, stage, tmp_path)
    assert result == 1
    assert record == {"category": category}
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == category + "\n"
    assert not any(path == "/private-redirect-target" for path, _ in stage[2])


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost",
        "https://user:pass@localhost",
        "https://localhost/path",
        "https://localhost?token=private",
        "https://localhost/#fragment",
        "https://localhost\\evil",
        "https://localhost:\n",
    ],
)
def test_invalid_origins_are_rejected_before_network(gate, tmp_path, url):
    result, record = invoke(gate, ("", {}, []), tmp_path, url=url)
    assert result == 1
    assert record == {"category": "stage_url_invalid"}


def test_cli_uses_environment_without_token_arguments(stage, tmp_path, monkeypatch):
    assert SCRIPT.exists(), "HTTPS staging preflight client is missing"
    monkeypatch.setenv("DROVER_TESTFLIGHT_STAGING_URL", stage[0])
    monkeypatch.setenv("DROVER_TESTFLIGHT_PREFLIGHT_TOKEN", TOKEN)
    result = subprocess.run(
        [
            "python3",
            str(SCRIPT),
            "--expected-sha",
            SHA,
            "--record",
            str(tmp_path / "record.json"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_network_timeout_is_ten_seconds_and_sanitized(gate, tmp_path, monkeypatch):
    class TimedOut:
        def open(self, request, timeout):
            assert timeout == 10
            raise TimeoutError("private URL and token")

    monkeypatch.setattr(gate, "build_opener", lambda *args: TimedOut())
    result, record = invoke(gate, ("https://example.invalid", {}, []), tmp_path)
    assert result == 1
    assert record == {"category": "stage_request_failed"}


def test_https_requires_a_trusted_certificate(gate, stage, tmp_path, monkeypatch):
    monkeypatch.delenv("SSL_CERT_FILE")
    result, record = invoke(gate, stage, tmp_path)
    assert result == 1
    assert record == {"category": "stage_request_failed"}
    assert stage[2] == []


def test_existing_record_is_never_overwritten(gate, stage, tmp_path, capsys):
    record = tmp_path / "preflight.json"
    record.write_text("previous candidate")
    result = gate.main(
        [
            "--url",
            stage[0],
            "--token",
            TOKEN,
            "--expected-sha",
            SHA,
            "--record",
            str(record),
        ]
    )
    assert result == 1
    assert record.read_text() == "previous candidate"
    assert capsys.readouterr().err == "stage_record_failed\n"
