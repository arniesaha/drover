"""A cockpit listener that survives its bind address coming and going (#457).

The hub used to bind ``[server].metrics_host`` exactly once. The installer
writes the detected private address there, which on most fleets is an overlay
VPN address, and that address disappears whenever the VPN client stops. Two
things then went wrong:

* Startup raised ``EADDRNOTAVAIL`` (Errno 49 on macOS, 99 on Linux). The API
  role exited, so launchd's ``KeepAlive`` relaunched it straight into the same
  error until it throttled the job; the all-in-one role logged the error and
  carried on with no HTTP listener at all, never trying again.
* A running hub kept a socket listening on an address no host interface owned.
  It was not busy - nothing could reach it. A local probe of that address is
  routed off the machine and times out instead of being refused, which is why
  ``/healthz`` looked hung at ~1% CPU.

``ResilientListener`` keeps the process up instead: a missing address is
retried with capped backoff and logged, a specific address that disappears
while serving has its listener closed, and the listener is rebound when the
address returns. A wildcard bind is never affected by an interface change and
is what the multi-interface docs recommend.
"""

from __future__ import annotations

import errno
import ipaddress
import logging
import socket
import socketserver
import threading
from collections.abc import Callable
from http.server import ThreadingHTTPServer
from typing import Any

log = logging.getLogger("drover.metrics")

#: First retry delay after a bind fails because the address is not there yet.
BIND_RETRY_INITIAL_SECONDS = 1.0
#: Backoff ceiling. Low enough that a returning VPN is served again within
#: half a minute, high enough that a long outage logs about twice a minute.
BIND_RETRY_MAX_SECONDS = 30.0
#: How often a listener on a specific address checks that it still exists.
ADDRESS_CHECK_SECONDS = 5.0

#: Bind failures that a changing network fixes by itself. Anything else
#: (a privileged port, a malformed host) is a configuration error and is
#: raised to the caller exactly as before.
_RETRYABLE_ERRNOS = frozenset(
    {
        errno.EADDRNOTAVAIL,
        errno.EADDRINUSE,
        errno.ENETDOWN,
        errno.ENETUNREACH,
        errno.EHOSTUNREACH,
    }
)


class DroverHTTPServer(ThreadingHTTPServer):
    """``ThreadingHTTPServer`` without the reverse DNS lookup in ``server_bind``.

    ``HTTPServer.server_bind`` calls ``socket.getfqdn(host)`` on the bind
    address. Nothing here reads ``server_name``, and that lookup blocks for the
    resolver's full timeout when the DNS server lives on the VPN that just
    went away - the moment a hub most needs to come back quickly.
    """

    def server_bind(self) -> None:
        # Deliberately skips HTTPServer.server_bind; see the class docstring.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = port


def is_retryable_bind_error(exc: BaseException) -> bool:
    """True when a bind failed because of the network, not the configuration."""
    if isinstance(exc, socket.gaierror):
        # A hostname whose resolver is on the missing interface.
        return True
    return isinstance(exc, OSError) and exc.errno in _RETRYABLE_ERRNOS


def _is_wildcard_or_loopback(host: str) -> bool:
    if host in {"", "0.0.0.0", "::", "*", "localhost"}:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


def address_is_local(host: str) -> bool:
    """Whether a specific IP literal is currently assigned to this machine.

    Binding a throwaway socket to an ephemeral port is the portable test and
    involves no DNS. Wildcard, loopback and non-literal hosts report True:
    the first two cannot disappear, and resolving a name here would add
    exactly the kind of blocking lookup this module exists to avoid.
    """
    if _is_wildcard_or_loopback(host):
        return True
    try:
        family = (
            socket.AF_INET6
            if isinstance(ipaddress.ip_address(host), ipaddress.IPv6Address)
            else socket.AF_INET
        )
    except ValueError:
        return True
    try:
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.bind((host, 0))
    except OSError as exc:
        return exc.errno != errno.EADDRNOTAVAIL
    return True


class ResilientListener:
    """Own one HTTP listener for the life of the process.

    ``factory`` builds and binds a fresh, not-yet-serving server; it is called
    again for every rebind. The object exposes the subset of the server API the
    runtime uses (``server_address``, ``shutdown``, ``server_close``).
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        factory: Callable[[], Any],
        name: str = "cockpit",
        initial_delay: float | None = None,
        max_delay: float | None = None,
        check_interval: float | None = None,
        address_available: Callable[[str], bool] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.name = name
        self._factory = factory
        # Resolved at construction rather than in the signature so the module
        # constants stay the single source of truth.
        self._initial_delay = (
            BIND_RETRY_INITIAL_SECONDS if initial_delay is None else initial_delay
        )
        self._max_delay = BIND_RETRY_MAX_SECONDS if max_delay is None else max_delay
        self._check_interval = (
            ADDRESS_CHECK_SECONDS if check_interval is None else check_interval
        )
        self._address_available = address_available or address_is_local
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._server: Any | None = None
        self._serve_thread: threading.Thread | None = None
        self._supervisor: threading.Thread | None = None
        self.bound = threading.Event()

    # -- public surface ------------------------------------------------------

    def start(self) -> "ResilientListener":
        """Bind now if possible; otherwise keep retrying in the background.

        A non-network bind error is raised here, synchronously, so the caller
        reports a misconfiguration the same way it always has.
        """
        server = None
        try:
            server = self._factory()
        except Exception as exc:  # noqa: BLE001 - classified below
            if not is_retryable_bind_error(exc):
                raise
            self._log_bind_failure(exc, attempt=1, delay=self._initial_delay)
        if server is not None:
            self._serve(server)
        self._supervisor = threading.Thread(
            target=self._supervise,
            args=(server is None,),
            name=f"drover-{self.name}-listener",
            daemon=True,
        )
        self._supervisor.start()
        return self

    @property
    def server_address(self) -> tuple[str, int]:
        with self._lock:
            server = self._server
        if server is not None:
            return server.server_address[:2]
        return (self.host, self.port)

    @property
    def server(self) -> Any | None:
        with self._lock:
            return self._server

    def shutdown(self) -> None:
        self._stop.set()
        self._close_current()
        if self._supervisor is not None and self._supervisor is not (
            threading.current_thread()
        ):
            self._supervisor.join(timeout=5.0)

    def server_close(self) -> None:
        # Shutdown already closed the socket; kept so the runtime can treat
        # this object exactly like the server it replaces.
        self._close_current()

    # -- internals -----------------------------------------------------------

    def _serve(self, server: Any) -> bool:
        thread = threading.Thread(
            target=server.serve_forever, name=f"drover-{self.name}", daemon=True
        )
        with self._lock:
            # Checked under the lock so a concurrent shutdown() either sees
            # this server and closes it, or this sees the stop and drops it.
            if self._stop.is_set():
                server.server_close()
                return False
            self._server = server
            self._serve_thread = thread
            thread.start()
        self.bound.set()
        return True

    def _close_current(self) -> None:
        with self._lock:
            server, thread = self._server, self._serve_thread
            self._server = self._serve_thread = None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=5.0)
        # Only once the socket is really closed, so `bound` never claims less
        # than the kernel is still accepting.
        self.bound.clear()

    def _log_bind_failure(self, exc: BaseException, *, attempt: int, delay: float):
        log.warning(
            "%s HTTP cannot bind %s:%s (%s); retrying in %.0fs (attempt %d). "
            "The process stays up and serves as soon as the address is "
            "available; bind to 0.0.0.0 to serve every interface.",
            self.name,
            self.host,
            self.port,
            exc,
            delay,
            attempt,
        )

    def _bind_with_backoff(self, attempts: int) -> Any | None:
        """Retry until bound or stopped. ``attempts`` already failed."""
        delay = self._initial_delay
        while not self._stop.wait(delay):
            attempts += 1
            try:
                server = self._factory()
            except Exception as exc:  # noqa: BLE001 - classified below
                if not is_retryable_bind_error(exc):
                    log.exception(
                        "%s HTTP bind to %s:%s failed permanently; not retrying",
                        self.name,
                        self.host,
                        self.port,
                    )
                    return None
                delay = min(delay * 2, self._max_delay)
                self._log_bind_failure(exc, attempt=attempts, delay=delay)
                continue
            log.info(
                "%s HTTP bound to %s:%s after %d attempt(s)",
                self.name,
                self.host,
                self.port,
                attempts,
            )
            return server
        return None

    def _supervise(self, needs_bind: bool) -> None:
        while not self._stop.is_set():
            if needs_bind:
                server = self._bind_with_backoff(attempts=1)
                if server is None or not self._serve(server):
                    return
            # Serving. Only a specific address can vanish from under us.
            while not self._stop.wait(self._check_interval):
                if not self._address_available(self.host):
                    log.warning(
                        "%s HTTP bind address %s is no longer assigned to this "
                        "machine; closing the listener and rebinding when it "
                        "returns",
                        self.name,
                        self.host,
                    )
                    self._close_current()
                    break
            else:
                return
            needs_bind = True
