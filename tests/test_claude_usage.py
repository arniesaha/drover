from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

from drover.server.providers.claude import ClaudeUsageProbe


def _credentials(tmp_path, *, expires_at_ms: int = 4102444800000):
    path = tmp_path / ".credentials.json"
    path.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-test-token",
                    "expiresAt": expires_at_ms,
                    "subscriptionType": "max",
                }
            }
        )
    )
    return path


USAGE_BODY = json.dumps(
    {
        "five_hour": {"utilization": 34.5, "resets_at": "2026-08-09T20:00:00Z"},
        "seven_day": {"utilization": 12.0, "resets_at": "2026-08-14T00:00:00Z"},
    }
).encode()


def test_usage_response_becomes_windows(tmp_path, monkeypatch):
    # The probe reads ANTHROPIC_BASE_URL, so a developer or CI box that has it
    # set (this repo proxied through it historically) would otherwise fail this
    # assertion for an unrelated reason.
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    calls = []

    def opener(url, headers, timeout):
        calls.append((url, headers, timeout))
        return 200, USAGE_BODY

    probe = ClaudeUsageProbe(
        credentials_path=_credentials(tmp_path),
        opener=opener,
        keychain_reader=lambda: None,
    )
    snapshot = probe.read(host_id="mac-mini")

    assert snapshot.status == "ok"
    assert snapshot.provider == "anthropic"
    assert snapshot.host_id == "mac-mini"
    assert snapshot.error_category is None
    kinds = {w.kind: w for w in snapshot.windows}
    assert kinds["five_hour"].used_percent == 34.5
    assert kinds["five_hour"].window_minutes == 300
    assert kinds["five_hour"].resets_at == datetime(2026, 8, 9, 20, tzinfo=timezone.utc)
    assert kinds["seven_day"].window_minutes == 10080
    url, headers, timeout = calls[0]
    assert url == "https://api.anthropic.com/api/oauth/usage"
    assert headers["Authorization"] == "Bearer sk-test-token"
    assert timeout == 5.0


def test_unknown_window_passes_through_without_a_guessed_duration(tmp_path):
    body = json.dumps(
        {
            "seven_day_something_new": {"utilization": 5.0, "resets_at": None},
        }
    ).encode()
    probe = ClaudeUsageProbe(
        credentials_path=_credentials(tmp_path),
        opener=lambda url, headers, timeout: (200, body),
        keychain_reader=lambda: None,
    )

    snapshot = probe.read(host_id="nas")

    window = snapshot.windows[0]
    assert window.kind == "seven_day_something_new"
    assert window.used_percent == 5.0
    assert window.window_minutes is None


def test_the_access_token_never_appears_in_the_snapshot(tmp_path):
    probe = ClaudeUsageProbe(
        credentials_path=_credentials(tmp_path),
        opener=lambda url, headers, timeout: (200, USAGE_BODY),
        keychain_reader=lambda: None,
    )

    snapshot = probe.read(host_id="mac-mini")

    assert "sk-test-token" not in json.dumps(
        {
            "label": snapshot.account_label,
            "plan": snapshot.plan_label,
            "dedup": snapshot.dedup_key,
            "source": snapshot.source,
            "error": snapshot.error_category,
        }
    )


def test_out_of_range_utilization_returns_error_snapshot(tmp_path):
    body = json.dumps(
        {
            "five_hour": {"utilization": 150, "resets_at": "2026-08-09T20:00:00Z"},
        }
    ).encode()
    probe = ClaudeUsageProbe(
        credentials_path=_credentials(tmp_path),
        opener=lambda url, headers, timeout: (200, body),
        keychain_reader=lambda: None,
    )

    snapshot = probe.read(host_id="mac-mini")

    assert snapshot.status == "error"
    assert snapshot.error_category == "protocol_error"
    assert snapshot.windows == ()


def test_nan_utilization_returns_error_snapshot(tmp_path):
    # json.loads accepts NaN as valid JSON
    body = b'{"five_hour": {"utilization": NaN, "resets_at": "2026-08-09T20:00:00Z"}}'
    probe = ClaudeUsageProbe(
        credentials_path=_credentials(tmp_path),
        opener=lambda url, headers, timeout: (200, body),
        keychain_reader=lambda: None,
    )

    snapshot = probe.read(host_id="mac-mini")

    assert snapshot.status == "error"
    assert snapshot.error_category == "protocol_error"
    assert snapshot.windows == ()


def test_absurd_expires_at_returns_error_snapshot(tmp_path):
    probe = ClaudeUsageProbe(
        credentials_path=_credentials(tmp_path, expires_at_ms=10**20),
        opener=lambda url, headers, timeout: (200, USAGE_BODY),
        keychain_reader=lambda: None,
    )

    snapshot = probe.read(host_id="mac-mini")

    assert snapshot.status == "error"
    assert snapshot.error_category == "protocol_error"
    assert snapshot.windows == ()


def test_missing_credentials_file_is_unavailable_not_an_error(tmp_path):
    probe = ClaudeUsageProbe(
        credentials_path=tmp_path / "absent.json",
        opener=lambda url, headers, timeout: pytest.fail("must not call the network"),
        keychain_reader=lambda: None,
    )

    snapshot = probe.read(host_id="work-laptop")

    assert snapshot.status == "usage_unavailable"
    assert snapshot.error_category == "not_authenticated"
    assert snapshot.windows == ()


def test_expired_token_short_circuits_before_spending_a_request(tmp_path):
    calls = []
    probe = ClaudeUsageProbe(
        credentials_path=_credentials(tmp_path, expires_at_ms=1000),
        opener=lambda url, headers, timeout: calls.append(url) or (200, USAGE_BODY),
        keychain_reader=lambda: None,
    )

    snapshot = probe.read(host_id="mac-mini")

    assert snapshot.status == "usage_unavailable"
    assert snapshot.error_category == "token_expired"
    # The point of this test: no request was made at all.
    assert calls == []


@pytest.mark.parametrize("code", [401, 403])
def test_rejected_credentials_are_unavailable_not_an_error(tmp_path, code):
    probe = ClaudeUsageProbe(
        credentials_path=_credentials(tmp_path),
        opener=lambda url, headers, timeout: (code, b"{}"),
        keychain_reader=lambda: None,
    )

    snapshot = probe.read(host_id="mac-mini")

    assert snapshot.status == "usage_unavailable"
    assert snapshot.error_category == "not_authenticated"


def test_timeout_is_an_error(tmp_path):
    def opener(url, headers, timeout):
        raise TimeoutError("timed out")

    probe = ClaudeUsageProbe(
        credentials_path=_credentials(tmp_path),
        opener=opener,
        keychain_reader=lambda: None,
    )

    snapshot = probe.read(host_id="mac-mini")

    assert snapshot.status == "error"
    assert snapshot.error_category == "timeout"


def test_connection_failure_is_an_error(tmp_path):
    def opener(url, headers, timeout):
        raise OSError("connection refused")

    probe = ClaudeUsageProbe(
        credentials_path=_credentials(tmp_path),
        opener=opener,
        keychain_reader=lambda: None,
    )

    snapshot = probe.read(host_id="mac-mini")

    assert snapshot.status == "error"
    assert snapshot.error_category == "unavailable"


def test_unparseable_body_is_a_protocol_error(tmp_path):
    probe = ClaudeUsageProbe(
        credentials_path=_credentials(tmp_path),
        opener=lambda url, headers, timeout: (200, b"<html>nope</html>"),
        keychain_reader=lambda: None,
    )

    snapshot = probe.read(host_id="mac-mini")

    assert snapshot.status == "error"
    assert snapshot.error_category == "protocol_error"


def test_a_parseable_response_with_no_windows_stays_quiet(tmp_path):
    """An empty account and a moved endpoint look identical from here, and the
    quieter reading is the right default for an undocumented API."""
    probe = ClaudeUsageProbe(
        credentials_path=_credentials(tmp_path),
        opener=lambda url, headers, timeout: (200, b"{}"),
        keychain_reader=lambda: None,
    )

    snapshot = probe.read(host_id="mac-mini")

    assert snapshot.status == "usage_unavailable"
    assert snapshot.error_category == "no_usage_reported"


def test_base_url_override_is_honoured(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9999")
    seen = []
    probe = ClaudeUsageProbe(
        credentials_path=_credentials(tmp_path),
        opener=lambda url, headers, timeout: seen.append(url) or (200, USAGE_BODY),
        keychain_reader=lambda: None,
    )

    probe.read(host_id="mac-mini")

    assert seen == ["http://127.0.0.1:9999/api/oauth/usage"]


KEYCHAIN_BODY = (
    json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": "sk-keychain-token",
                "expiresAt": 4102444800000,
                "subscriptionType": "max",
            }
        }
    )
    .encode()
    .decode()
)


def test_keychain_wins_when_the_file_token_has_expired(tmp_path):
    """The bug this fixes: on macOS the file is a stale leftover and its token
    expired days ago, while the live credential sits in the Keychain."""
    seen = {}

    def opener(url, headers, timeout):
        seen["auth"] = headers["Authorization"]
        return 200, USAGE_BODY

    probe = ClaudeUsageProbe(
        credentials_path=_credentials(tmp_path, expires_at_ms=1000),
        opener=opener,
        keychain_reader=lambda: KEYCHAIN_BODY,
    )

    snapshot = probe.read(host_id="mac-mini")

    assert snapshot.status == "ok"
    assert seen["auth"] == "Bearer sk-keychain-token"


def test_the_file_is_used_when_the_keychain_is_empty(tmp_path):
    seen = {}

    def opener(url, headers, timeout):
        seen["auth"] = headers["Authorization"]
        return 200, USAGE_BODY

    probe = ClaudeUsageProbe(
        credentials_path=_credentials(tmp_path),
        opener=opener,
        keychain_reader=lambda: None,
    )

    snapshot = probe.read(host_id="nas")

    assert snapshot.status == "ok"
    assert seen["auth"] == "Bearer sk-test-token"


def test_both_sources_expired_reports_token_expired(tmp_path):
    expired_keychain = json.dumps(
        {"claudeAiOauth": {"accessToken": "sk-old", "expiresAt": 1000}}
    )
    probe = ClaudeUsageProbe(
        credentials_path=_credentials(tmp_path, expires_at_ms=1000),
        opener=lambda url, headers, timeout: pytest.fail("must not call the network"),
        keychain_reader=lambda: expired_keychain,
    )

    snapshot = probe.read(host_id="mac-mini")

    assert snapshot.status == "usage_unavailable"
    assert snapshot.error_category == "token_expired"


def test_no_source_at_all_reports_not_authenticated(tmp_path):
    probe = ClaudeUsageProbe(
        credentials_path=tmp_path / "absent.json",
        opener=lambda url, headers, timeout: pytest.fail("must not call the network"),
        keychain_reader=lambda: None,
    )

    snapshot = probe.read(host_id="work-laptop")

    assert snapshot.status == "usage_unavailable"
    assert snapshot.error_category == "not_authenticated"


def test_a_keychain_that_prompts_or_fails_does_not_break_the_probe(tmp_path):
    """A daemon without the Keychain grant must look like a host that was never
    signed in, not like breakage, and must never hang the refresh cycle."""

    def reader():
        raise TimeoutError("security prompted and we gave up")

    probe = ClaudeUsageProbe(
        credentials_path=tmp_path / "absent.json",
        opener=lambda url, headers, timeout: pytest.fail("must not call the network"),
        keychain_reader=reader,
    )

    snapshot = probe.read(host_id="mac-mini")

    assert snapshot.status == "usage_unavailable"
    assert snapshot.error_category == "not_authenticated"


def test_the_keychain_secret_never_reaches_the_snapshot(tmp_path):
    probe = ClaudeUsageProbe(
        credentials_path=tmp_path / "absent.json",
        opener=lambda url, headers, timeout: (200, USAGE_BODY),
        keychain_reader=lambda: KEYCHAIN_BODY,
    )

    snapshot = probe.read(host_id="mac-mini")

    assert "sk-keychain-token" not in json.dumps(
        {
            "label": snapshot.account_label,
            "plan": snapshot.plan_label,
            "dedup": snapshot.dedup_key,
            "source": snapshot.source,
            "error": snapshot.error_category,
        }
    )


def test_unreadable_credentials_path_is_a_protocol_error_not_absent(tmp_path):
    """A directory at credentials_path raises IsADirectoryError on read_text.
    That is a present-but-broken source, not an absent one -- it must not be
    silently downgraded to not_authenticated. Using a directory instead of
    chmod 000 keeps this deterministic regardless of the test runner's
    privileges."""
    credentials_dir = tmp_path / ".credentials.json"
    credentials_dir.mkdir()
    probe = ClaudeUsageProbe(
        credentials_path=credentials_dir,
        opener=lambda url, headers, timeout: pytest.fail("must not call the network"),
        keychain_reader=lambda: None,
    )

    snapshot = probe.read(host_id="mac-mini")

    assert snapshot.status == "error"
    assert snapshot.error_category == "protocol_error"


def test_invalid_json_in_the_file_is_a_protocol_error(tmp_path):
    path = tmp_path / ".credentials.json"
    path.write_text("not json")
    probe = ClaudeUsageProbe(
        credentials_path=path,
        opener=lambda url, headers, timeout: pytest.fail("must not call the network"),
        keychain_reader=lambda: None,
    )

    snapshot = probe.read(host_id="mac-mini")

    assert snapshot.status == "error"
    assert snapshot.error_category == "protocol_error"


def test_missing_file_with_empty_keychain_still_reports_not_authenticated(tmp_path):
    """Guard against over-correcting the protocol_error fix: a genuinely
    absent file (FileNotFoundError) must still fall through to the next
    source, not be treated as an error."""
    probe = ClaudeUsageProbe(
        credentials_path=tmp_path / "absent.json",
        opener=lambda url, headers, timeout: pytest.fail("must not call the network"),
        keychain_reader=lambda: None,
    )

    snapshot = probe.read(host_id="mac-mini")

    assert snapshot.status == "usage_unavailable"
    assert snapshot.error_category == "not_authenticated"
