"""Tests for drover.server.web.credentials -- issue, verify, revoke, persist."""

from __future__ import annotations

import json
import stat

import pytest
from click.testing import CliRunner

from drover.config import default_config
from drover.server.__main__ import main
from drover.server.web.auth import AuthSettings
from drover.server.web.credentials import (
    CREDENTIALS_FILENAME,
    Credential,
    CredentialStore,
    verifier_from_token,
)

PUBLIC_CREDENTIAL_KEYS = {
    "id",
    "scope",
    "label",
    "created_at",
    "host_id",
    "last_used_at",
    "revoked_at",
}


def _store(tmp_path) -> CredentialStore:
    return CredentialStore(tmp_path / CREDENTIALS_FILENAME)


def test_verifier_is_deterministic_and_token_bound():
    assert verifier_from_token("abc") == verifier_from_token("abc")
    assert verifier_from_token("abc") != verifier_from_token("abd")


def test_issue_returns_token_and_stores_only_the_verifier(tmp_path):
    store = _store(tmp_path)
    credential, token = store.issue(scope="device", label="Phone")

    assert credential.scope == "device"
    assert credential.label == "Phone"
    assert credential.is_active is True
    assert credential.verifier == verifier_from_token(token)

    raw = (tmp_path / CREDENTIALS_FILENAME).read_text(encoding="utf-8")
    assert token not in raw
    assert credential.verifier in raw


def test_issue_rejects_unknown_scope(tmp_path):
    with pytest.raises(ValueError):
        _store(tmp_path).issue(scope="admin", label="nope")


def test_issue_accepts_preflight_scope_and_persists_only_its_verifier(tmp_path):
    store = _store(tmp_path)
    credential, token = store.issue(scope="preflight", label="testflight-ci")

    assert credential.scope == "preflight"
    stored = (tmp_path / CREDENTIALS_FILENAME).read_text(encoding="utf-8")
    assert token not in stored
    assert credential.verifier in stored


def test_issue_preflight_cli_mints_through_the_running_server(monkeypatch, tmp_path):
    """The CLI is a different process from the hub that has to honour the token.

    CredentialStore loads once per process, so a credential written straight to
    the file is invisible to the running server -- and erased by its next write.
    """
    import drover.server.__main__ as server_main

    sent = {}

    def fake_request(cfg, method, path, payload=None):
        sent.update({"method": method, "path": path, "payload": payload})
        return {"token": "preflight-token", "credential_id": "cred-1"}

    monkeypatch.setattr(
        server_main, "_resolve_config", lambda path, **_kwargs: default_config()
    )
    monkeypatch.setattr(server_main, "_local_api_request", fake_request)

    result = CliRunner().invoke(
        main, ["credentials", "issue-preflight", "--label", "testflight-ci"]
    )

    assert result.exit_code == 0, result.output
    assert result.output == "preflight-token\n"
    assert sent == {
        "method": "POST",
        "path": "/auth/credentials",
        "payload": {"scope": "preflight", "label": "testflight-ci"},
    }


def test_issue_preflight_cli_never_writes_a_credential_file_itself(
    monkeypatch, tmp_path
):
    """No local store is touched, whichever config selected the hub."""
    import drover.server.__main__ as server_main

    home = tmp_path / "home"
    home.mkdir()
    config_path = home / "config.toml"
    config_path.write_text(
        '[auth]\nenabled = true\napi_token = "staging-token"\n', encoding="utf-8"
    )
    monkeypatch.setattr(
        server_main,
        "_local_api_request",
        lambda cfg, method, path, payload=None: {
            "token": "preflight-token",
            "credential_id": "cred-1",
        },
    )

    result = CliRunner().invoke(
        main,
        [
            "--config",
            str(config_path),
            "credentials",
            "issue-preflight",
            "--label",
            "testflight-ci",
        ],
    )

    assert result.exit_code == 0, result.output
    assert not (home / CREDENTIALS_FILENAME).exists()


def test_find_active_matches_only_the_issued_token(tmp_path):
    store = _store(tmp_path)
    credential, token = store.issue(scope="device", label="Phone")

    assert store.find_active(token).id == credential.id
    assert store.find_active(token + "x") is None


def test_revoke_makes_the_token_stop_working(tmp_path):
    store = _store(tmp_path)
    credential, token = store.issue(scope="device", label="Phone")

    assert store.revoke(credential.id) is True
    assert store.find_active(token) is None
    assert store.revoke(credential.id) is False, "revoking twice is not a change"
    assert store.list_all()[0].revoked_at is not None


def test_host_credential_carries_its_host_id(tmp_path):
    store = _store(tmp_path)
    credential, _ = store.issue(scope="host", label="build-mac", host_id="build-mac")
    assert credential.host_id == "build-mac"


def test_store_reloads_from_disk(tmp_path):
    store = _store(tmp_path)
    _, token = store.issue(scope="device", label="Phone")
    server_id = store.server_id

    reopened = _store(tmp_path)
    assert reopened.find_active(token) is not None
    assert reopened.server_id == server_id, "server_id must be stable across restarts"


def test_credentials_file_is_owner_only(tmp_path):
    store = _store(tmp_path)
    store.issue(scope="device", label="Phone")
    mode = (tmp_path / CREDENTIALS_FILENAME).stat().st_mode
    assert stat.S_IMODE(mode) == 0o600


def test_public_json_never_leaks_the_verifier(tmp_path):
    store = _store(tmp_path)
    credential, _ = store.issue(scope="device", label="Phone")
    public = credential.as_public_json()
    assert "verifier" not in public
    assert public["label"] == "Phone"


def test_public_json_is_an_explicit_allowlist(tmp_path):
    store = _store(tmp_path)
    credential, _ = store.issue(scope="device", label="Phone")
    assert store.set_apns_registration(
        credential.id, token="secret-device-token", environment="sandbox"
    )
    public = store.get(credential.id).as_public_json()
    assert set(public) == PUBLIC_CREDENTIAL_KEYS
    assert "secret-device-token" not in json.dumps(public)


def test_apns_registration_writes_inside_touch_debounce(tmp_path):
    store = _store(tmp_path)
    credential, _ = store.issue(scope="device", label="Phone")
    store.touch(credential.id, now=1000)
    assert store.set_apns_registration(
        credential.id, token="token-1", environment="sandbox"
    )
    assert store.set_apns_registration(
        credential.id, token="token-2", environment="production"
    )
    loaded = CredentialStore(tmp_path / CREDENTIALS_FILENAME).get(credential.id)
    assert (loaded.apns_token, loaded.apns_environment) == (
        "token-2",
        "production",
    )


def test_clear_apns_registration_compares_expected_token(tmp_path):
    store = _store(tmp_path)
    credential, _ = store.issue(scope="device", label="Phone")
    store.set_apns_registration(credential.id, token="new-token", environment="sandbox")
    assert not store.clear_apns_registration(credential.id, expected_token="old-token")
    assert store.get(credential.id).apns_token == "new-token"
    assert store.clear_apns_registration(credential.id, expected_token="new-token")
    assert store.get(credential.id).apns_token is None


def test_apns_rejection_is_recorded_without_the_token_and_survives_restart(tmp_path):
    store = _store(tmp_path)
    credential, _ = store.issue(scope="device", label="Phone")
    store.set_apns_registration(
        credential.id, token="dead-token", environment="sandbox"
    )

    assert not store.mark_apns_registration_failed(
        credential.id, expected_token="other-token", reason="BadDeviceToken"
    )
    assert store.mark_apns_registration_failed(
        credential.id, expected_token="dead-token", reason="BadDeviceToken"
    )

    loaded = CredentialStore(tmp_path / CREDENTIALS_FILENAME).get(credential.id)
    assert (loaded.apns_token, loaded.apns_environment) == (None, None)
    assert loaded.apns_failure_reason == "BadDeviceToken"
    assert loaded.apns_failed_at is not None
    assert loaded.apns_registration_rejected("dead-token", "sandbox")
    assert not loaded.apns_registration_rejected("dead-token", "production")
    assert "dead-token" not in (tmp_path / CREDENTIALS_FILENAME).read_text()
    assert set(loaded.as_public_json()) == PUBLIC_CREDENTIAL_KEYS

    assert store.set_apns_registration(
        credential.id, token="new-token", environment="sandbox"
    )
    renewed = store.get(credential.id)
    assert (renewed.apns_failure_reason, renewed.apns_failed_fingerprint) == (
        None,
        None,
    )


def test_revoke_destroys_apns_capability(tmp_path):
    store = _store(tmp_path)
    credential, _ = store.issue(scope="device", label="Phone")
    store.set_apns_registration(
        credential.id, token="device-token", environment="sandbox"
    )
    assert store.revoke(credential.id)
    revoked = store.get(credential.id)
    assert revoked.revoked_at is not None
    assert revoked.apns_token is None
    assert revoked.apns_environment is None


def test_apns_registration_rejects_invalid_environment_and_non_device(tmp_path):
    store = _store(tmp_path)
    host, _ = store.issue(scope="host", label="build-mac", host_id="build-mac")

    with pytest.raises(ValueError, match="unknown APNs environment"):
        store.set_apns_registration(
            host.id, token="device-token", environment="development"
        )
    assert not store.set_apns_registration(
        host.id, token="device-token", environment="sandbox"
    )


def test_touch_is_debounced(tmp_path):
    store = _store(tmp_path)
    credential, _ = store.issue(scope="device", label="Phone")

    store.touch(credential.id, now=1000.0)
    first = store.list_all()[0].last_used_at
    assert first is not None

    store.touch(credential.id, now=1000.5)
    assert store.list_all()[0].last_used_at == first, "debounced inside the window"

    store.touch(credential.id, now=1100.0)
    assert store.list_all()[0].last_used_at != first


def test_corrupt_file_does_not_crash_the_store(tmp_path):
    (tmp_path / CREDENTIALS_FILENAME).write_text("{not json", encoding="utf-8")
    store = _store(tmp_path)
    assert store.list_all() == []
    credential, token = store.issue(scope="device", label="Phone")
    assert store.find_active(token).id == credential.id


def test_unreadable_entries_are_skipped_not_fatal(tmp_path):
    (tmp_path / CREDENTIALS_FILENAME).write_text(
        json.dumps({"version": 1, "credentials": [{"id": "x"}, "junk"]}),
        encoding="utf-8",
    )
    assert CredentialStore(tmp_path / CREDENTIALS_FILENAME).list_all() == []


def test_host_issue_requires_binding_and_rotation_is_isolated(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="host_id"):
        store.issue(scope="host", label="legacy")
    first, old_token = store.issue(scope="host", label="a", host_id="a")
    _, other_token = store.issue(scope="host", label="b", host_id="b")
    second, new_token = store.issue(scope="host", label="a", host_id="a")
    reloaded = _store(tmp_path)
    assert reloaded.find_active(old_token) is None
    assert reloaded.get(first.id).revoked_at is not None
    assert reloaded.find_active(new_token).id == second.id
    assert reloaded.find_active(other_token).host_id == "b"


def test_upgrade_preserves_bound_credentials_and_requires_unbound_reissue(tmp_path):
    from drover.server.web.auth import (
        credential_allows_request,
        credential_matches_host,
    )

    store = _store(tmp_path)
    bound, bound_token = store.issue(scope="host", label="a", host_id="a")
    legacy, legacy_token = store.issue(scope="device", label="legacy")
    document = json.loads((tmp_path / CREDENTIALS_FILENAME).read_text())
    for item in document["credentials"]:
        if item["id"] == legacy.id:
            item["scope"] = "host"
            item.pop("host_id")
    (tmp_path / CREDENTIALS_FILENAME).write_text(json.dumps(document))
    upgraded = _store(tmp_path)
    assert credential_matches_host(upgraded.find_active(bound_token), "a")
    unbound = upgraded.find_active(legacy_token)
    assert not credential_matches_host(unbound, "legacy")
    assert not credential_allows_request(unbound)
    _, replacement = upgraded.issue(scope="host", label="legacy", host_id="legacy")
    upgraded.revoke(legacy.id)
    assert upgraded.find_active(legacy_token) is None
    assert credential_matches_host(upgraded.find_active(replacement), "legacy")
    assert upgraded.find_active(bound_token).id == bound.id
