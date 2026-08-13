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
    APNsConfig,
    APNsSender,
    AwaitingTransition,
    _AuthToken,
    configure,
    dispatch_awaiting_transition,
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
        "title": "claude-code needs you",
        "body": "drover — approval required",
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
    assert body == "drover — your turn"


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
    host, _ = store.issue(scope="host", label="nas")
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

    sender._deliver(_transition(preview="Ready to deploy. Want me to push?"))

    alert = json.loads(client.posts[0]["content"])["aps"]["alert"]
    assert alert["body"] == "Ready to deploy. Want me to push?"
    # Title still identifies the harness; the subtitle carries what the body
    # used to say, so nothing is lost by promoting the message.
    assert alert["title"] == "claude-code needs you"
    assert alert["subtitle"] == "drover · approval required"


def test_without_a_preview_the_old_wording_survives(tmp_path, config):
    store, _ = _paired_device(tmp_path)
    client = FakeClient()

    APNsSender(config, store, client=client)._deliver(_transition(preview=""))

    alert = json.loads(client.posts[0]["content"])["aps"]["alert"]
    assert alert["body"] == "drover — approval required"
    # No subtitle, because it would only repeat the body.
    assert "subtitle" not in alert


def test_markdown_is_flattened_for_a_lock_screen():
    from drover.server.push.apns import _condense

    assert _condense("**Done**\n- one\n- two") == "Done • one • two"
    # Backticks stay: `git push --force` still reads as a command, and
    # stripping them would change what the command looks like.
    assert _condense("Run `git push --force`") == "Run `git push --force`"


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
