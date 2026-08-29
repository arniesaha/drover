"""An attached relay socket is not a working host.

A spoke can complete the websocket upgrade and then send nothing at all: no
frames, not even a pong. The hub's silence watchdog does tear that connection
down, but only after SILENCE_TIMEOUT_S, and the spoke reconnects immediately
afterwards. Presence built on "is a socket attached" therefore reads `online`
almost continuously for a host that has not spoken in an hour, and session
creation is accepted against it, producing a session that reports `running`,
records zero events, and raises no error anywhere.

These tests pin the two places that knowledge has to reach: the status the
fleet renders, and the admission check on creating a session.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from drover.schema import bootstrap
from drover.server.harness.registry import HarnessRegistry
from drover.server.metrics import MetricsCollector, _harness_host_dict

HOST_TOKEN = "host-secret"
HOST_ID = "work-laptop"


class _Relay:
    """A relay manager in one of the three states presence has to tell apart."""

    def __init__(self, *, attached: bool, silent_for: float | None) -> None:
        self._attached = attached
        self._silent_for = silent_for
        self.waited: list[float] = []

    def is_live(self, host_id: str) -> bool:
        return self._attached

    def is_responsive(self, host_id: str) -> bool:
        return (
            self._attached and self._silent_for is not None and self._silent_for <= 60.0
        )

    def wait_until_responsive(self, host_id: str, timeout_s: float) -> bool:
        self.waited.append(timeout_s)
        return self.is_responsive(host_id)

    def silent_for(self, host_id: str) -> float | None:
        return self._silent_for if self._attached else None


def _relay_host(host_id: str = HOST_ID) -> SimpleNamespace:
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        host_id=host_id,
        display_name="Work Laptop",
        connection_kind="relay",
        status="online",
        last_seen_at=now,
        created_at=now,
        updated_at=now,
    )


# --- what the fleet renders ---------------------------------------------------


def test_an_attached_but_silent_spoke_reports_offline() -> None:
    """The #231 case: the socket is up, the host has not spoken in 50 minutes."""
    relay = _Relay(attached=True, silent_for=None)
    item = _harness_host_dict(_relay_host(), relay)
    assert item["status"] == "offline"


def test_a_spoke_that_has_gone_quiet_reports_offline() -> None:
    """Past the silence budget but not yet torn down is still not online."""
    relay = _Relay(attached=True, silent_for=3000.0)
    item = _harness_host_dict(_relay_host(), relay)
    assert item["status"] == "offline"


def test_a_talking_spoke_reports_online() -> None:
    relay = _Relay(attached=True, silent_for=1.0)
    item = _harness_host_dict(_relay_host(), relay)
    assert item["status"] == "online"


def test_no_relay_connection_reports_offline() -> None:
    relay = _Relay(attached=False, silent_for=None)
    assert _harness_host_dict(_relay_host(), relay)["status"] == "offline"
    assert _harness_host_dict(_relay_host(), None)["status"] == "offline"


def test_a_direct_host_is_untouched_by_relay_liveness() -> None:
    """Direct hosts keep their own age-based staleness rule."""
    now = datetime.now(timezone.utc)
    host = SimpleNamespace(
        host_id="mac-mini",
        connection_kind="direct",
        status="online",
        last_seen_at=now,
        created_at=now,
        updated_at=now,
    )
    item = _harness_host_dict(host, _Relay(attached=False, silent_for=None))
    assert item["status"] == "online"


# --- what session creation accepts --------------------------------------------


def _collector(tmp_path, *, connection_kind: str = "relay") -> MetricsCollector:
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    HarnessRegistry(duckdb_path).register_host(
        host_id=HOST_ID,
        display_name="Work Laptop",
        kind="macos",
        local_url=None,
        connection_kind=connection_kind,
        capabilities={"harnesses": [{"name": "claude", "enabled": True}]},
    )
    return MetricsCollector(
        duckdb_path=duckdb_path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        api_token=HOST_TOKEN,
    )


def _record_requests(collector: MetricsCollector) -> list:
    sent: list = []

    def fake_request(host, path, *, method="GET", payload=None, **kwargs):
        sent.append((host.host_id, method, path))
        return 200, '{"session_id": "s1"}\n'

    collector._harness_request = fake_request  # type: ignore[assignment]
    return sent


def test_creating_a_session_on_a_silent_spoke_is_refused(tmp_path) -> None:
    """Better a clear 502 than a session that starts and goes quiet."""
    collector = _collector(tmp_path)
    collector.relay_manager = _Relay(attached=True, silent_for=None)
    sent = _record_requests(collector)

    status, body = collector.proxy_create_harness_session(
        HOST_ID, {"harness": "claude"}
    )

    assert status == 502
    assert "not responding" in body
    assert "never sent a frame" in body
    assert sent == [], "the create must not be handed to a socket nobody reads"


def test_the_refusal_says_how_long_the_spoke_has_been_quiet(tmp_path) -> None:
    collector = _collector(tmp_path)
    collector.relay_manager = _Relay(attached=True, silent_for=93.0)
    _record_requests(collector)

    status, body = collector.proxy_create_harness_session(
        HOST_ID, {"harness": "claude"}
    )

    assert status == 502
    assert "no frames for 93s" in body


def test_creating_a_session_with_no_relay_connection_is_refused(tmp_path) -> None:
    collector = _collector(tmp_path)
    collector.relay_manager = _Relay(attached=False, silent_for=None)
    _record_requests(collector)

    status, body = collector.proxy_create_harness_session(
        HOST_ID, {"harness": "claude"}
    )

    assert status == 502
    assert "no relay connection is attached" in body


def test_a_responsive_spoke_still_gets_its_session(tmp_path) -> None:
    """The regression guard: this must not become a gate on working hosts."""
    collector = _collector(tmp_path)
    collector.relay_manager = _Relay(attached=True, silent_for=2.0)
    sent = _record_requests(collector)

    status, _ = collector.proxy_create_harness_session(HOST_ID, {"harness": "claude"})

    assert status == 200
    assert sent == [(HOST_ID, "POST", "/sessions")]


def test_a_direct_host_is_not_subject_to_the_relay_check(tmp_path) -> None:
    """Direct hosts have no relay socket to be responsive on."""
    collector = _collector(tmp_path, connection_kind="direct")
    collector.relay_manager = _Relay(attached=False, silent_for=None)
    sent = _record_requests(collector)

    status, _ = collector.proxy_create_harness_session(HOST_ID, {"harness": "claude"})

    assert status == 200
    assert sent == [(HOST_ID, "POST", "/sessions")]


def test_an_unknown_host_is_still_a_404(tmp_path) -> None:
    collector = _collector(tmp_path)
    collector.relay_manager = _Relay(attached=False, silent_for=None)
    status, body = collector.proxy_create_harness_session("ghost", {})
    assert status == 404
    assert "unknown harness host" in body
