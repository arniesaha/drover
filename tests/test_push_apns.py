"""Tests for drover.server.push.apns -- signing, gating, payload, and 410.

The provider JWT is verified against the public half of a throwaway P-256 key
rather than asserted on shape alone: a DER-wrapped signature is the same
length class as a raw one and still fails at Apple with a bare 403, so only a
real verification proves the encoding.
"""

from __future__ import annotations

import base64
import json

import pytest

from drover.server.push.apns import (
    DELIVERED,
    DEVICE_REJECTED,
    ENVIRONMENT_REJECTED,
    REJECTED,
    TRANSIENT,
    APNsConfig,
    APNsSender,
    AwaitingTransition,
    _AuthToken,
    classify_response,
    configure,
    dispatch_awaiting_transition,
    push_available,
    push_status,
    set_sender,
)
from drover.server.web.credentials import CredentialStore

cryptography = pytest.importorskip("cryptography")

from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.utils import (  # noqa: E402
    encode_dss_signature,
)


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@pytest.fixture
def signing_key(tmp_path):
    """A real P-256 key on disk, shaped exactly like an Apple .p8."""
    key = ec.generate_private_key(ec.SECP256R1())
    path = tmp_path / "AuthKey_TESTKEY123.p8"
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return key, path


@pytest.fixture
def config(signing_key):
    _, path = signing_key
    return APNsConfig(
        enabled=True,
        key_path=path,
        key_id="TESTKEY123",
        team_id="TEAMID1234",
        bundle_id="com.arnab.drover",
    )


class FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


class FakeClient:
    """Records posts; returns queued responses (200 once exhausted)."""

    def __init__(self, responses=None):
        self.posts = []
        self._responses = list(responses or [])

    def post(self, url, content=None, headers=None):
        self.posts.append({"url": url, "content": content, "headers": headers})
        if self._responses:
            return self._responses.pop(0)
        return FakeResponse(200)

    def close(self):
        pass


def _paired_device(tmp_path, *, environment="sandbox", token="devicetoken123"):
    store = CredentialStore(tmp_path / "credentials.json")
    credential, _ = store.issue(scope="device", label="iPhone")
    store.set_apns_registration(credential.id, token=token, environment=environment)
    return store, credential


def _transition(**overrides):
    fields = {
        "session_id": "sess-1",
        "harness": "claude-code",
        "cwd": "/Users/x/work/drover",
        "awaiting": "approval",
    }
    fields.update(overrides)
    return AwaitingTransition(**fields)


# --- signing ---------------------------------------------------------------


def test_provider_token_is_a_verifiable_es256_jwt(signing_key, config):
    key, _ = signing_key
    header, claims, signature = _AuthToken(config).value(now=1000).split(".")

    assert json.loads(_b64url_decode(header)) == {
        "alg": "ES256",
        "kid": "TESTKEY123",
    }
    assert json.loads(_b64url_decode(claims)) == {"iss": "TEAMID1234", "iat": 1000}

    raw = _b64url_decode(signature)
    # JWS ES256 is a raw r||s pair over P-256, never the DER encoding
    # `cryptography` hands back from sign().
    assert len(raw) == 64
    key.public_key().verify(
        encode_dss_signature(
            int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")
        ),
        f"{header}.{claims}".encode("ascii"),
        ec.ECDSA(hashes.SHA256()),
    )


def test_provider_token_is_cached_then_refreshed(config):
    auth = _AuthToken(config)
    first = auth.value(now=1000)

    # Apple rate-limits a provider that re-mints more than once per 20 min.
    assert auth.value(now=1000 + 40 * 60) == first
    assert auth.value(now=1000 + 50 * 60) != first


# --- configuration gating --------------------------------------------------


@pytest.mark.parametrize(
    "override",
    [
        {"enabled": False},
        {"key_id": ""},
        {"team_id": ""},
        {"bundle_id": ""},
    ],
)
def test_incomplete_config_is_not_usable(config, override):
    from dataclasses import replace

    assert not replace(config, **override).is_usable


def test_missing_key_file_is_not_usable(config, tmp_path):
    from dataclasses import replace

    assert not replace(config, key_path=tmp_path / "absent.p8").is_usable


def test_configure_disables_cleanly_when_key_is_absent(tmp_path, caplog):
    class Cfg:
        apns_enabled = True
        apns_key_path = str(tmp_path / "absent.p8")
        apns_key_id = "K"
        apns_team_id = "T"
        apns_bundle_id = "com.arnab.drover"

    store = CredentialStore(tmp_path / "credentials.json")
    assert configure(Cfg(), store) is None
    # Enabled-but-broken must be loud, not silent: it is the difference
    # between "push is off" and "push is on and losing every alert".
    assert any("not usable" in record.message for record in caplog.records)
    assert not push_available()


def test_push_available_reports_registered_usable_sender(config, tmp_path):
    store = CredentialStore(tmp_path / "credentials.json")
    sender = APNsSender(config, store)
    try:
        set_sender(sender)
        assert push_available()
    finally:
        sender.close()
        set_sender(None)


# --- delivery --------------------------------------------------------------


def test_alert_carries_time_sensitive_payload_and_collapse_id(tmp_path, config):
    store, credential = _paired_device(tmp_path)
    client = FakeClient()
    sender = APNsSender(config, store, client=client)

    sender._deliver(_transition())

    assert len(client.posts) == 1
    post = client.posts[0]
    assert post["url"] == "https://api.sandbox.push.apple.com/3/device/devicetoken123"
    assert post["headers"]["apns-topic"] == "com.arnab.drover"
    assert post["headers"]["apns-push-type"] == "alert"
    assert post["headers"]["apns-priority"] == "10"
    # Collapsing on the session means an offline phone wakes to one banner per
    # session, not one per transition it missed.
    assert post["headers"]["apns-collapse-id"] == "sess-1"
    assert post["headers"]["authorization"].startswith("bearer ")

    aps = json.loads(post["content"])["aps"]
    assert aps["alert"] == {
        "title": "drover",
        "body": "Needs your input: approve a command",
    }
    assert aps["interruption-level"] == "time-sensitive"


def test_production_environment_uses_the_production_host(tmp_path, config):
    store, _ = _paired_device(tmp_path, environment="production")
    client = FakeClient()

    APNsSender(config, store, client=client)._deliver(_transition())

    assert client.posts[0]["url"].startswith("https://api.push.apple.com/")


def test_input_and_approval_read_differently(tmp_path, config):
    store, _ = _paired_device(tmp_path)
    client = FakeClient()
    sender = APNsSender(config, store, client=client)

    sender._deliver(_transition(awaiting="input"))

    body = json.loads(client.posts[0]["content"])["aps"]["alert"]["body"]
    assert body == "Needs your input: reply in the app"


def test_gone_response_clears_the_registration(tmp_path, config):
    store, credential = _paired_device(tmp_path)
    client = FakeClient([FakeResponse(410, "Unregistered")])

    APNsSender(config, store, client=client)._deliver(_transition())

    # A dead token must not survive to cost a send on every later transition.
    assert store.get(credential.id).apns_token is None
    assert store.get(credential.id).apns_environment is None


def test_other_failures_leave_the_registration_intact(tmp_path, config):
    store, credential = _paired_device(tmp_path)
    client = FakeClient([FakeResponse(503, "ServiceUnavailable")])

    APNsSender(config, store, client=client)._deliver(_transition())

    # A transient Apple outage is not evidence the phone is gone.
    assert store.get(credential.id).apns_token == "devicetoken123"


# --- rejection classification ----------------------------------------------


def _reason(reason):
    return json.dumps({"reason": reason})


@pytest.mark.parametrize(
    "status, body, kind",
    [
        (200, "", DELIVERED),
        (400, _reason("BadDeviceToken"), DEVICE_REJECTED),
        (400, _reason("DeviceTokenNotForTopic"), DEVICE_REJECTED),
        (410, _reason("Unregistered"), DEVICE_REJECTED),
        (410, _reason("ExpiredToken"), DEVICE_REJECTED),
        (410, "", DEVICE_REJECTED),
        (403, _reason("BadEnvironmentKeyInToken"), ENVIRONMENT_REJECTED),
        (403, _reason("InvalidProviderToken"), ENVIRONMENT_REJECTED),
        (403, _reason("ExpiredProviderToken"), ENVIRONMENT_REJECTED),
        (403, _reason("MissingProviderToken"), ENVIRONMENT_REJECTED),
        (400, _reason("TopicDisallowed"), ENVIRONMENT_REJECTED),
        (400, _reason("BadTopic"), ENVIRONMENT_REJECTED),
        (429, _reason("TooManyRequests"), TRANSIENT),
        (429, _reason("TooManyProviderTokenUpdates"), TRANSIENT),
        (500, _reason("InternalServerError"), TRANSIENT),
        (503, _reason("ServiceUnavailable"), TRANSIENT),
        (503, "not json", TRANSIENT),
        (413, _reason("PayloadTooLarge"), REJECTED),
        (400, _reason("BadCollapseId"), REJECTED),
        (403, "", REJECTED),
    ],
)
def test_responses_are_classified_by_what_they_condemn(status, body, kind):
    assert classify_response(status, body).kind == kind


def test_classification_keeps_apples_reason():
    outcome = classify_response(403, _reason("BadEnvironmentKeyInToken"))

    assert (outcome.status, outcome.reason) == (403, "BadEnvironmentKeyInToken")


@pytest.mark.parametrize(
    "status, reason",
    [
        (400, "BadDeviceToken"),
        (400, "DeviceTokenNotForTopic"),
        (410, "Unregistered"),
    ],
)
def test_device_rejection_clears_and_records_the_registration(
    tmp_path, config, caplog, status, reason
):
    store, credential = _paired_device(tmp_path, environment="production")
    client = FakeClient([FakeResponse(status, _reason(reason))])
    sender = APNsSender(config, store, client=client)

    sender._deliver(_transition())

    stored = store.get(credential.id)
    assert (stored.apns_token, stored.apns_environment) == (None, None)
    assert stored.apns_failure_reason == reason
    assert stored.apns_failed_at is not None
    # The same token re-sent by the app is recognisably the rejected one...
    assert stored.apns_registration_rejected("devicetoken123", "production")
    # ...but not when it is reported for the environment that issued it.
    assert not stored.apns_registration_rejected("devicetoken123", "sandbox")
    # One dead phone says nothing about anyone else's.
    assert sender.environment_available("production")
    assert "devicetoken123" not in caplog.text
    assert "devicetoken123" not in (tmp_path / "credentials.json").read_text()


def test_device_rejection_does_not_condemn_a_token_registered_since(tmp_path, config):
    store, credential = _paired_device(tmp_path)

    class ReRegisteringClient(FakeClient):
        def post(self, url, content=None, headers=None):
            # The phone re-registered while this send was in flight.
            store.set_apns_registration(
                credential.id, token="fresh-token", environment="sandbox"
            )
            return FakeResponse(400, _reason("BadDeviceToken"))

    APNsSender(config, store, client=ReRegisteringClient())._deliver(_transition())

    stored = store.get(credential.id)
    assert stored.apns_token == "fresh-token"
    assert stored.apns_failure_reason is None


@pytest.mark.parametrize(
    "reason",
    ["BadEnvironmentKeyInToken", "InvalidProviderToken", "ExpiredProviderToken"],
)
def test_environment_rejection_flips_availability_for_that_environment(
    tmp_path, config, caplog, reason
):
    store, credential = _paired_device(tmp_path, environment="production")
    sandbox_phone, _ = store.issue(scope="device", label="Dev phone")
    store.set_apns_registration(
        sandbox_phone.id, token="sandboxtoken", environment="sandbox"
    )
    client = FakeClient([FakeResponse(403, _reason(reason))])
    sender = APNsSender(config, store, client=client)
    try:
        set_sender(sender)

        sender._deliver(_transition())

        assert not push_available("production")
        assert push_available("sandbox")
        assert push_available()
        status = push_status()
        assert status["state"] == "degraded"
        assert status["environments"]["sandbox"] == {"available": True}
        production = status["environments"]["production"]
        assert production["available"] is False
        assert (production["status"], production["reason"]) == (403, reason)
        assert production["since"]
        assert any(
            record.levelname == "ERROR" and reason in record.message
            for record in caplog.records
        )
        assert "devicetoken123" not in caplog.text
    finally:
        sender.close()
        set_sender(None)


def test_environment_rejection_stops_sends_and_logs_once(tmp_path, config, caplog):
    store, _ = _paired_device(tmp_path, environment="production")
    second, _ = store.issue(scope="device", label="iPad")
    store.set_apns_registration(second.id, token="ipadtoken", environment="production")
    dev, _ = store.issue(scope="device", label="Dev phone")
    store.set_apns_registration(dev.id, token="sandboxtoken", environment="sandbox")
    client = FakeClient([FakeResponse(403, _reason("BadEnvironmentKeyInToken"))])
    sender = APNsSender(config, store, client=client)

    sender._deliver(_transition())
    sender._deliver(_transition(session_id="sess-2"))

    production_posts = [
        post
        for post in client.posts
        if post["url"].startswith("https://api.push.apple.com/")
    ]
    # The first production send condemned the environment; nothing after it
    # paid for another send that could never land. Sandbox carried on.
    assert len(production_posts) == 1
    sandbox_posts = [post for post in client.posts if "sandbox" in post["url"]]
    assert len(sandbox_posts) == 2
    errors = [record for record in caplog.records if record.levelname == "ERROR"]
    assert len(errors) == 1


def test_environment_rejection_keeps_registrations_for_a_fixed_restart(
    tmp_path, config
):
    store, credential = _paired_device(tmp_path, environment="production")
    client = FakeClient([FakeResponse(403, _reason("InvalidProviderToken"))])

    APNsSender(config, store, client=client)._deliver(_transition())

    # The phone did nothing wrong; once the operator fixes the key and
    # restarts, its registration must work without waiting for a re-pair.
    stored = store.get(credential.id)
    assert stored.apns_token == "devicetoken123"
    assert stored.apns_failure_reason is None
    assert APNsSender(config, store).environment_available("production")


@pytest.mark.parametrize(
    "status, body",
    [
        (429, _reason("TooManyRequests")),
        (500, _reason("InternalServerError")),
        (503, _reason("ServiceUnavailable")),
    ],
)
def test_transient_failures_keep_registrations_and_availability(
    tmp_path, config, status, body
):
    store, credential = _paired_device(tmp_path, environment="production")
    client = FakeClient([FakeResponse(status, body)])
    sender = APNsSender(config, store, client=client)

    sender._deliver(_transition())

    stored = store.get(credential.id)
    assert stored.apns_token == "devicetoken123"
    assert stored.apns_failure_reason is None
    assert sender.environment_available("production")


def test_timeouts_keep_registrations_and_availability(tmp_path, config):
    import httpx

    class TimingOutClient:
        def post(self, *a, **k):
            raise httpx.ReadTimeout("timed out")

    store, credential = _paired_device(tmp_path)
    sender = APNsSender(config, store, client=TimingOutClient())

    sender._deliver(_transition())

    assert store.get(credential.id).apns_token == "devicetoken123"
    assert sender.environment_available("sandbox")


def test_repeated_unclassified_rejections_are_rate_limited(tmp_path, config, caplog):
    store, _ = _paired_device(tmp_path)
    client = FakeClient(
        [FakeResponse(413, _reason("PayloadTooLarge")) for _ in range(3)]
    )
    sender = APNsSender(config, store, client=client)

    for index in range(3):
        sender._deliver(_transition(session_id=f"sess-{index}"))

    assert len(client.posts) == 3
    warnings = [r for r in caplog.records if "PayloadTooLarge" in r.message]
    assert len(warnings) == 1


def test_push_status_reports_disabled_without_a_sender():
    set_sender(None)

    assert push_status() == {"state": "disabled"}
    assert not push_available("production")


def test_transport_failure_is_swallowed(tmp_path, config):
    class ExplodingClient:
        def post(self, *a, **k):
            raise OSError("network down")

    store, credential = _paired_device(tmp_path)

    APNsSender(config, store, client=ExplodingClient())._deliver(_transition())

    assert store.get(credential.id).apns_token == "devicetoken123"


def test_unregistered_and_revoked_devices_are_skipped(tmp_path, config):
    store = CredentialStore(tmp_path / "credentials.json")
    # never registered for push
    store.issue(scope="device", label="no-token phone")
    # a host, not a phone
    host, _ = store.issue(scope="host", label="nas", host_id="nas")
    # revoked after registering
    revoked, _ = store.issue(scope="device", label="lost phone")
    store.set_apns_registration(revoked.id, token="dead", environment="sandbox")
    store.revoke(revoked.id)

    client = FakeClient()
    APNsSender(config, store, client=client)._deliver(_transition())

    assert client.posts == []


def test_notify_ignores_transitions_that_do_not_need_the_user(tmp_path, config):
    store, _ = _paired_device(tmp_path)
    client = FakeClient()
    sender = APNsSender(config, store, client=client)

    # The session went back to working: nothing to tell the user about.
    sender.notify(_transition(awaiting=None))
    sender._pool.shutdown(wait=True)

    assert client.posts == []


def test_notify_is_inert_when_config_is_unusable(tmp_path, config):
    from dataclasses import replace

    store, _ = _paired_device(tmp_path)
    client = FakeClient()
    sender = APNsSender(replace(config, enabled=False), store, client=client)

    sender.notify(_transition())
    sender._pool.shutdown(wait=True)

    assert client.posts == []


# --- module-level dispatch -------------------------------------------------


def test_dispatch_without_a_sender_is_a_no_op():
    set_sender(None)
    dispatch_awaiting_transition(_transition())  # must not raise


def test_dispatch_survives_a_broken_sender():
    class Broken:
        def notify(self, transition):
            raise RuntimeError("boom")

    set_sender(Broken())
    try:
        # Recording harness activity must never fail because push is broken.
        dispatch_awaiting_transition(_transition())
    finally:
        set_sender(None)


# --- notification body ------------------------------------------------------


def test_body_quotes_the_agent_rather_than_a_generic_phrase(tmp_path, config):
    store, _ = _paired_device(tmp_path)
    client = FakeClient()
    sender = APNsSender(config, store, client=client)

    sender._deliver(
        _transition(awaiting="input", preview="Ready to deploy. Want me to push?")
    )

    alert = json.loads(client.posts[0]["content"])["aps"]["alert"]
    assert alert["body"] == "Needs your input: Ready to deploy. Want me to push?"
    assert alert["title"] == "drover"
    assert "subtitle" not in alert


def test_without_a_preview_the_old_wording_survives(tmp_path, config):
    store, _ = _paired_device(tmp_path)
    client = FakeClient()

    APNsSender(config, store, client=client)._deliver(_transition(preview=""))

    alert = json.loads(client.posts[0]["content"])["aps"]["alert"]
    assert alert["body"] == "Needs your input: approve a command"
    # No subtitle, because it would only repeat the body.
    assert "subtitle" not in alert


def test_markdown_is_flattened_for_a_lock_screen():
    from drover.server.push.apns import _condense

    assert _condense("**Done**\n- one\n- two") == "Done • one • two"
    assert _condense("Run `git push --force`") == "Run git push --force"


def test_long_messages_are_cut_on_a_word_boundary():
    from drover.server.push.apns import _SUMMARY_MAX_CHARS, _condense

    body = _condense("word " * 200)

    assert len(body) <= _SUMMARY_MAX_CHARS + 1  # + the ellipsis
    assert body.endswith("…")
    assert "wor…" not in body  # never mid-word


def test_a_cut_landing_on_a_sentence_end_gets_no_ellipsis():
    from drover.server.push.apns import _condense

    text = ("alpha " * 34) + "end. " + ("beta " * 40)
    body = _condense(text)

    # "…the end.…" reads like a typo rather than a truncation.
    assert not body.endswith(".…")


def test_blank_previews_never_produce_a_body_of_whitespace():
    from drover.server.push.apns import _condense

    assert _condense("   \n\t ") == ""
    assert _condense(None) == ""


class ManualTimer:
    def __init__(self, delay, callback):
        self.delay = delay
        self.callback = callback
        self.daemon = False

    def start(self):
        pass

    def cancel(self):
        pass


def _batch_sender(config, store):
    now = [100.0]
    client = FakeClient()
    sender = APNsSender(
        config, store, client=client, clock=lambda: now[0], timer_factory=ManualTimer
    )
    return sender, client, now


@pytest.mark.parametrize(
    "mode,awaiting,status,allowed",
    [
        ("action", "input", "running", True),
        ("action", "approval", "running", True),
        ("action", None, "completed", True),
        ("action", None, "failed", True),
        ("action", None, "errored", True),
        ("action", None, "running", False),
        ("action", None, "terminated", False),
        ("all", None, "running", True),
        ("digest", "input", "running", True),
        ("digest", None, "running", False),
    ],
)
def test_delivery_mode_transition_filter(mode, awaiting, status, allowed):
    assert _transition(awaiting=awaiting, status=status).allowed(mode) is allowed


@pytest.mark.parametrize("status", ["completed", "failed", "errored"])
def test_terminal_payload_is_normal_and_threaded(tmp_path, config, status):
    store, _ = _paired_device(tmp_path)
    sender = APNsSender(config, store, client=FakeClient())
    payload = json.loads(
        sender._payload(
            _transition(
                status=status, title="OpenClaw capture", preview="Ready for review"
            ),
            None,
        )
    )
    assert payload["aps"]["interruption-level"] == "active"
    assert payload["aps"]["thread-id"] == "sess-1"
    assert payload["aps"]["alert"]["title"] == "OpenClaw capture"
    assert "subtitle" not in payload["aps"]["alert"]
    sender.close()


def test_notification_text_strips_machine_details_and_limits_length(tmp_path, config):
    store, _ = _paired_device(tmp_path)
    sender = APNsSender(config, store)
    state = _transition(
        session_id="harness-12345678-1234-1234-1234-123456789abc",
        awaiting="input",
        title="codex harness-abcdef123 run-123456789 deadbeef Fix capture",
        preview="harness-12345678-1234-1234-1234-123456789abc deadbeef run-123 Ready "
        + "word " * 100,
    )
    alert = json.loads(sender._payload(state, None))["aps"]["alert"]
    assert alert["title"] == "Fix capture"
    assert len(alert["body"]) <= 220
    assert all(
        word not in str(alert)
        for word in ("harness", "deadbeef", "run-", "codex", "12345678")
    )
    sender.close()


def test_private_content_never_appears_in_alert(tmp_path, config):
    store, _ = _paired_device(tmp_path)
    sender = APNsSender(config, store)
    alert = json.loads(
        sender._payload(
            _transition(
                awaiting="input", private=True, title="secret", preview="secret"
            ),
            None,
        )
    )["aps"]["alert"]
    assert alert == {"title": "drover", "body": "Needs your input: reply in the app"}
    sender.close()


def test_collapse_id_is_byte_bounded_and_collision_resistant():
    from drover.server.push.apns import _collapse_id

    a = "é" * 100
    assert len(_collapse_id(a).encode()) == 64
    assert _collapse_id(a) != _collapse_id(a + "x")


def test_batch_has_fixed_window_and_latest_state_per_session(tmp_path, config):
    store, _ = _paired_device(tmp_path)
    sender, client, now = _batch_sender(config, store)
    sender.notify(_transition(status="completed"))
    now[0] = 159
    sender.notify(_transition(session_id="sess-2", status="completed"))
    sender.notify(_transition(session_id="sess-2", status="failed"))
    sender.flush_due()
    assert client.posts == []
    now[0] = 160
    sender.flush_due()
    assert len(client.posts) == 1
    payload = json.loads(client.posts[0]["content"])
    assert payload["session_ids"] == ["sess-1", "sess-2"]
    assert payload["aps"]["alert"]["body"] == "1 session finished, 1 session failed"
    assert payload["aps"]["interruption-level"] == "active"
    sender.notify(_transition(status="completed"))
    now[0] = 220
    sender.flush_due()
    assert len(client.posts) == 2
    assert json.loads(client.posts[1]["content"])["session_id"] == "sess-1"
    sender.close()


def test_resolved_input_is_removed_before_batch_delivery(tmp_path, config):
    store, _ = _paired_device(tmp_path)
    sender, client, now = _batch_sender(config, store)
    sender.notify(_transition())
    sender.notify(_transition(awaiting=None))
    now[0] = 160
    sender.flush_due()
    assert client.posts == []
    sender.close()


def test_each_device_has_its_own_mode_and_digest_deadline(tmp_path, config):
    store, first = _paired_device(tmp_path)
    second, _ = store.issue(scope="device", label="Tablet")
    store.set_apns_registration(second.id, token="tablet", environment="sandbox")
    store.set_notification_mode(second.id, "digest")
    sender, client, now = _batch_sender(config, store)
    sender.notify(_transition())
    now[0] = 160
    sender.flush_due()
    assert len(client.posts) == 1
    assert client.posts[0]["url"].endswith("devicetoken123")
    now[0] = 86400
    sender.flush_due()
    assert len(client.posts) == 2
    assert client.posts[1]["url"].endswith("tablet")
    assert (
        json.loads(client.posts[1]["content"])["aps"]["interruption-level"] == "active"
    )
    sender.close()


def test_delivery_rechecks_revocation_and_preference(tmp_path, config):
    store, device = _paired_device(tmp_path)
    sender, client, now = _batch_sender(config, store)
    sender.notify(_transition())
    store.set_notification_mode(device.id, "digest")
    now[0] = 160
    sender.flush_due()
    assert client.posts == []
    sender.notify(_transition())
    store.revoke(device.id)
    now[0] = 86400
    sender.flush_due()
    assert client.posts == []
    sender.close()


def test_git_delivery_tail_stays_in_the_app():
    state = _transition(
        awaiting=None,
        status="completed",
        preview="Capture is ready for review\nPushed deadbeef to feat/capture\nCommit abcdef123",
    )
    assert state.alert_body() == "Finished: Capture is ready for review"


def test_large_batches_keep_payloads_under_apns_limit(tmp_path, config):
    store, _ = _paired_device(tmp_path)
    sender, client, now = _batch_sender(config, store)
    for i in range(45):
        sender.notify(
            _transition(
                session_id=f"harness-00000000-0000-0000-0000-{i:012d}",
                status="completed",
            )
        )
    now[0] = 160
    sender.flush_due()
    assert len(client.posts) == 3
    assert all(len(post["content"]) <= 4096 for post in client.posts)
    assert (
        sum(len(json.loads(post["content"])["session_ids"]) for post in client.posts)
        == 45
    )
    sender.close()


def test_all_updates_device_receives_progress_while_default_device_does_not(
    tmp_path, config
):
    store, _ = _paired_device(tmp_path)
    second, _ = store.issue(scope="device", label="Tablet")
    store.set_apns_registration(second.id, token="tablet", environment="sandbox")
    store.set_notification_mode(second.id, "all")
    sender, client, now = _batch_sender(config, store)
    sender.notify(_transition(awaiting=None, status="running"))
    now[0] = 160
    sender.flush_due()
    assert len(client.posts) == 1
    assert client.posts[0]["url"].endswith("tablet")
    assert (
        json.loads(client.posts[0]["content"])["aps"]["interruption-level"] == "active"
    )
    sender.close()


@pytest.mark.parametrize("mode", ["action", "all"])
@pytest.mark.parametrize("awaiting", ["input", "approval"])
def test_needs_input_shortens_existing_batch_and_includes_pending_sessions(
    tmp_path, config, mode, awaiting
):
    store, device = _paired_device(tmp_path)
    store.set_notification_mode(device.id, mode)
    sender, client, now = _batch_sender(config, store)
    sender.notify(_transition(session_id="finished", status="completed"))
    now[0] = 110
    sender.notify(_transition(session_id="waiting", awaiting=awaiting))
    now[0] = 114
    sender.notify(_transition(session_id="failed", status="failed"))
    sender.flush_due()
    assert client.posts == []
    now[0] = 115
    sender.flush_due()
    assert len(client.posts) == 1
    payload = json.loads(client.posts[0]["content"])
    assert payload["session_ids"] == ["failed", "finished", "waiting"]
    assert (
        payload["aps"]["alert"]["body"]
        == "1 session finished, 1 session needs input, 1 session failed"
    )
    assert payload["aps"]["interruption-level"] == "time-sensitive"
    sender.close()


def test_repeated_urgent_states_do_not_extend_short_window(tmp_path, config):
    store, _ = _paired_device(tmp_path)
    sender, client, now = _batch_sender(config, store)
    sender.notify(_transition(awaiting="approval"))
    now[0] = 104
    sender.notify(_transition(awaiting="input"))
    sender.flush_due()
    assert client.posts == []
    now[0] = 105
    sender.flush_due()
    assert len(client.posts) == 1
    sender.close()
