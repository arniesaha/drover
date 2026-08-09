from __future__ import annotations

from datetime import datetime, timezone
import json

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

    probe = ClaudeUsageProbe(credentials_path=_credentials(tmp_path), opener=opener)
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
    )

    snapshot = probe.read(host_id="mac-mini")

    assert snapshot.status == "error"
    assert snapshot.error_category == "protocol_error"
    assert snapshot.windows == ()


def test_absurd_expires_at_returns_error_snapshot(tmp_path):
    probe = ClaudeUsageProbe(
        credentials_path=_credentials(tmp_path, expires_at_ms=10**20),
        opener=lambda url, headers, timeout: (200, USAGE_BODY),
    )

    snapshot = probe.read(host_id="mac-mini")

    assert snapshot.status == "error"
    assert snapshot.error_category == "protocol_error"
    assert snapshot.windows == ()
