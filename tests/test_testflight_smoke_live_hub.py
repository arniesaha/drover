"""Live-hub smoke contracts: only GETs, bounded waits, no credential output."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from testflight import smoke_live_hub as smoke


@pytest.fixture
def hub():
    requests = []
    responses = {
        "/healthz": (200, b"ok\n"),
        "/readyz": (200, b'{"ready":true}'),
        "/harness/hosts": (200, b'{"hosts":[]}'),
        "/harness/sessions": (200, b'{"sessions":[]}'),
    }
    delays = {}
    header = ["ok"]

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(
                (self.command, self.path, self.headers.get("Authorization"))
            )
            time.sleep(max(0, delays.get(self.path, 0)))
            status, body = responses[self.path]
            self.send_response(status)
            self.send_header("X-Drover-Analytical", header[0])
            self.send_header("Location", "/redirect-target")
            self.end_headers()
            try:
                if delays.get(self.path, 0) < 0:
                    for byte in body:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(-delays[self.path])
                else:
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", requests, responses, delays, header
    server.shutdown()
    server.server_close()
    thread.join()


def test_success_checks_only_gets_and_authenticates_listings(hub):
    url, requests, _, _, _ = hub
    assert smoke.smoke(url, "sentinel") == {"ok": True, "checks": 4, "analytical": "ok"}
    assert requests == [
        ("GET", "/healthz", None),
        ("GET", "/readyz", "Bearer sentinel"),
        ("GET", "/harness/hosts", "Bearer sentinel"),
        ("GET", "/harness/sessions", "Bearer sentinel"),
    ]


@pytest.mark.parametrize("path", smoke.PATHS)
@pytest.mark.parametrize("status", [302, 401, 503])
def test_http_failure_or_redirect_stops_without_following(hub, path, status):
    url, requests, responses, _, _ = hub
    responses[path] = (status, b"sentinel")
    with pytest.raises(smoke.Rejected, match="request_failed"):
        smoke.smoke(url, "sentinel")
    assert requests[-1][1] == path
    assert len(requests) == smoke.PATHS.index(path) + 1


@pytest.mark.parametrize(
    "path,body,category",
    [
        ("/healthz", b"not ok", "health_invalid"),
        ("/healthz", b" ok ", "health_invalid"),
        ("/readyz", b'{"ready":false}', "not_ready"),
        ("/readyz", b'{"ready":1}', "not_ready"),
        ("/readyz", b"[]", "not_ready"),
        ("/readyz", b"sentinel", "readiness_invalid"),
        (
            "/harness/sessions",
            b"x" * (smoke.MAX_RESPONSE_BYTES + 1),
            "response_too_large",
        ),
    ],
)
def test_bad_health_readiness_or_oversized_response(hub, path, body, category):
    url, _, responses, _, _ = hub
    responses[path] = (200, body)
    with pytest.raises(smoke.Rejected, match=category):
        smoke.smoke(url, "sentinel")


def test_total_budget_is_shared_by_all_requests(hub):
    url, _, _, delays, _ = hub
    delays.update({path: 0.04 for path in smoke.PATHS})
    start = time.monotonic()
    with pytest.raises(smoke.Rejected, match="budget_exceeded"):
        smoke.smoke(url, "sentinel", budget=0.1)
    assert time.monotonic() - start < 0.3


@pytest.mark.parametrize(
    "url",
    [
        None,
        "ftp://hub.example",
        "http://user:sentinel@hub.example",
        "https://hub.example/path",
        "https://hub.example?sentinel",
        "https://hub.example#",
        "http://hub.example:",
    ],
)
def test_bad_origins_are_rejected(url):
    with pytest.raises(smoke.Rejected, match="url_invalid"):
        smoke.smoke(url, "sentinel")


@pytest.mark.parametrize("token", [None, "", "bad\ntoken"])
def test_missing_or_invalid_environment_token(hub, token):
    with pytest.raises(smoke.Rejected, match="credential_invalid"):
        smoke.smoke(hub[0], token)
    assert hub[1] == []


@pytest.mark.parametrize("budget", [0, -1, float("inf"), float("nan")])
def test_invalid_budget(hub, budget):
    with pytest.raises(smoke.Rejected, match="budget_invalid"):
        smoke.smoke(hub[0], "sentinel", budget)


def test_cli_output_never_contains_token_url_or_response(hub, monkeypatch, capsys):
    url, _, responses, _, header = hub
    monkeypatch.setenv("DROVER_TESTFLIGHT_HUB_URL", url)
    monkeypatch.setenv("DROVER_TESTFLIGHT_HUB_TOKEN", "sentinel")
    header[0] = "sentinel"
    assert smoke.main([]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["analytical"] == "unreported"
    responses["/harness/hosts"] = (401, b"sentinel")
    assert smoke.main([]) == 1
    captured = capsys.readouterr()
    assert captured.err == "live hub smoke failed: request_failed\n"
    assert smoke.main(["--token", "sentinel"]) == 1
    captured = capsys.readouterr()
    assert "sentinel" not in captured.out + captured.err
    assert url not in captured.out + captured.err


def test_cli_wall_deadline_covers_a_blocked_fetch(hub, monkeypatch, capsys):
    url, _, _, delays, _ = hub
    monkeypatch.setenv("DROVER_TESTFLIGHT_HUB_TOKEN", "sentinel")
    delays["/healthz"] = 0.4
    start = time.monotonic()
    assert smoke.main(["--url", url, "--budget-seconds", "0.05"]) == 1
    assert time.monotonic() - start < 0.3
    assert capsys.readouterr().err == "live hub smoke failed: budget_exceeded\n"


def test_cli_wall_deadline_stops_a_trickling_body(hub, monkeypatch, capsys):
    url, _, _, delays, _ = hub
    monkeypatch.setenv("DROVER_TESTFLIGHT_HUB_TOKEN", "sentinel")
    delays["/healthz"] = -0.04
    start = time.monotonic()
    assert smoke.main(["--url", url, "--budget-seconds", "0.06"]) == 1
    assert time.monotonic() - start < 0.15
    assert capsys.readouterr().err == "live hub smoke failed: budget_exceeded\n"


@pytest.mark.parametrize("status", ["ok", "recovering", "failed-retrying"])
def test_reports_known_analytical_header_independently_of_readiness(hub, status):
    url, _, _, _, header = hub
    header[0] = status
    assert smoke.smoke(url, "sentinel")["analytical"] == status
