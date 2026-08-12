"""Tests for drover.server.web.credentials -- issue, verify, revoke, persist."""

from __future__ import annotations

import json
import stat

import pytest

from drover.server.web.credentials import (
    CREDENTIALS_FILENAME,
    Credential,
    CredentialStore,
    verifier_from_token,
)


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
