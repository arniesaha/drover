"""The hub's listener survives its bind address coming and going (#457).

A hub bound to an overlay VPN address used to exit (API role) or run on with
no listener (all-in-one role) when that address was missing at startup, and a
running hub kept listening on an address the machine no longer had. These pin
the replacement behaviour: stay up, log, retry with capped backoff, rebind.

``192.0.2.1`` is TEST-NET-1 (RFC 5737): never assigned to a real interface, so
binding it produces the same kernel ``EADDRNOTAVAIL`` a vanished VPN does.
"""

from __future__ import annotations

import errno
import logging
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler

import pytest

from drover.server.web import app as web_app
from drover.server.web import listener as listener_mod
from drover.server.web.listener import (
    DroverHTTPServer,
    ResilientListener,
    address_is_local,
    is_retryable_bind_error,
)

_MISSING_ADDRESS = "192.0.2.1"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _healthz(port: int, timeout: float = 2.0) -> int:
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/healthz", timeout=timeout
    ) as response:
        return response.status


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class _Collector:
    relay_manager = None

    def __init__(self, tmp_path) -> None:
        self.duckdb_path = tmp_path / "drover.duckdb"


class _OkHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib method name
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_args) -> None:
        return None


def _missing_address_error() -> OSError:
    try:
        with socket.socket() as sock:
            sock.bind((_MISSING_ADDRESS, 0))
    except OSError as exc:
        return exc
    pytest.skip(f"{_MISSING_ADDRESS} is assigned on this machine")


def test_missing_bind_address_is_detected_without_dns():
    assert address_is_local("127.0.0.1")
    assert address_is_local("0.0.0.0")
    assert address_is_local("::")
    # Names are never resolved by the watchdog; they cannot be judged missing.
    assert address_is_local("hub.example.invalid")
    assert not address_is_local(_MISSING_ADDRESS)


def test_only_network_bind_failures_are_retried():
    assert is_retryable_bind_error(_missing_address_error())
    assert is_retryable_bind_error(OSError(errno.EADDRINUSE, "in use"))
    assert is_retryable_bind_error(socket.gaierror(8, "nodename nor servname"))
    assert not is_retryable_bind_error(OSError(errno.EACCES, "denied"))
    assert not is_retryable_bind_error(ValueError("bad host"))


def test_bind_does_not_reverse_resolve_the_address(monkeypatch):
    """HTTPServer.server_bind calls getfqdn, which stalls when DNS is on the VPN."""

    def fail(*_args, **_kwargs):
        raise AssertionError("server_bind must not do a DNS lookup")

    monkeypatch.setattr(socket, "getfqdn", fail)
    server = DroverHTTPServer(("127.0.0.1", 0), _OkHandler)
    try:
        assert server.server_name == "127.0.0.1"
        assert server.server_port == server.server_address[1]
    finally:
        server.server_close()


def test_hub_stays_up_when_bind_address_is_missing_and_serves_once_it_appears(
    monkeypatch, tmp_path, caplog
):
    """Acceptance: address missing at startup -> stays up, logs, binds later."""
    monkeypatch.setattr(listener_mod, "BIND_RETRY_INITIAL_SECONDS", 0.05)
    monkeypatch.setattr(listener_mod, "BIND_RETRY_MAX_SECONDS", 0.2)
    port = _free_port()
    appeared = threading.Event()
    attempts: list[str] = []
    real_server = web_app.DroverHTTPServer

    def server_for_current_network(address, handler):
        # Until the "VPN" comes up the configured address really is absent,
        # so the kernel raises exactly what the reference hub hit.
        attempts.append("bind")
        host = "127.0.0.1" if appeared.is_set() else _MISSING_ADDRESS
        return real_server((host, address[1]), handler)

    monkeypatch.setattr(web_app, "DroverHTTPServer", server_for_current_network)
    caplog.set_level(logging.INFO, logger="drover.metrics")

    hub = web_app.start_resilient_metrics_server(
        host="127.0.0.1", port=port, collector=_Collector(tmp_path)
    )
    try:
        assert not hub.bound.is_set()
        assert _wait_until(lambda: len(attempts) >= 3)
        with pytest.raises(urllib.error.URLError):
            _healthz(port, timeout=0.5)
        assert "cannot bind" in caplog.text
        assert "retrying in" in caplog.text

        appeared.set()
        assert hub.bound.wait(5.0)
        assert _healthz(port) == 200
        assert "bound to 127.0.0.1" in caplog.text
    finally:
        hub.shutdown()
        hub.server_close()


def test_configuration_errors_still_fail_startup():
    def denied():
        raise OSError(errno.EACCES, "Permission denied")

    with pytest.raises(OSError) as raised:
        ResilientListener(host="127.0.0.1", port=80, factory=denied).start()
    assert raised.value.errno == errno.EACCES


def test_retry_backoff_is_capped(monkeypatch, caplog):
    error = _missing_address_error()
    waits: list[float] = []

    class RecordingStop(threading.Event):
        def wait(self, timeout=None):
            if timeout is not None:
                waits.append(timeout)
            return len(waits) > 8 or self.is_set()

    def missing():
        raise error

    listener = ResilientListener(
        host=_MISSING_ADDRESS,
        port=7080,
        factory=missing,
        initial_delay=1.0,
        max_delay=8.0,
    )
    listener._stop = RecordingStop()
    caplog.set_level(logging.WARNING, logger="drover.metrics")
    listener.start()
    listener._supervisor.join(timeout=5.0)

    assert not listener._supervisor.is_alive()
    assert waits[:8] == [1.0, 2.0, 4.0, 8.0, 8.0, 8.0, 8.0, 8.0]
    assert caplog.text.count("cannot bind") >= 8


def test_vanished_address_closes_listener_and_rebinds_when_it_returns(caplog):
    """Documented manual check, automated: the interface going away mid-run
    neither wedges the process nor needs a restart once it is back."""
    port = _free_port()
    present = threading.Event()
    present.set()
    listener = ResilientListener(
        host="127.0.0.1",
        port=port,
        factory=lambda: DroverHTTPServer(("127.0.0.1", port), _OkHandler),
        initial_delay=0.05,
        max_delay=0.1,
        check_interval=0.05,
        address_available=lambda _host: present.is_set(),
    )
    caplog.set_level(logging.INFO, logger="drover.metrics")
    listener.start()
    try:
        assert _healthz(port) == 200

        present.clear()
        assert _wait_until(lambda: not listener.bound.is_set())
        assert "no longer assigned" in caplog.text
        # Gone means refused promptly, not a request that hangs.
        started = time.monotonic()
        with pytest.raises(urllib.error.URLError):
            _healthz(port, timeout=2.0)
        assert time.monotonic() - started < 1.5

        present.set()
        assert listener.bound.wait(5.0)
        assert _healthz(port) == 200
    finally:
        listener.shutdown()
        listener.server_close()


def test_healthz_answers_while_a_slow_request_is_in_flight(tmp_path):
    """Liveness is never queued behind other work on the listener."""
    release = threading.Event()

    class SlowThenOk(_OkHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib method name
            if self.path == "/slow":
                release.wait(5.0)
            super().do_GET()

    port = _free_port()
    listener = ResilientListener(
        host="127.0.0.1",
        port=port,
        factory=lambda: DroverHTTPServer(("127.0.0.1", port), SlowThenOk),
    ).start()
    slow = threading.Thread(
        target=lambda: urllib.request.urlopen(
            f"http://127.0.0.1:{port}/slow", timeout=10
        ).close(),
        daemon=True,
    )
    try:
        slow.start()
        time.sleep(0.1)
        assert _healthz(port, timeout=1.0) == 200
    finally:
        release.set()
        slow.join(timeout=5.0)
        listener.shutdown()


def test_shutdown_stops_a_listener_that_is_still_waiting_for_its_address():
    error = _missing_address_error()

    def missing():
        raise error

    listener = ResilientListener(
        host=_MISSING_ADDRESS, port=7080, factory=missing, initial_delay=10.0
    ).start()
    started = time.monotonic()
    listener.shutdown()
    listener.server_close()
    assert time.monotonic() - started < 2.0
    assert not listener._supervisor.is_alive()
    assert listener.server_address == (_MISSING_ADDRESS, 7080)
