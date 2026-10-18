"""Advisory-content routes through a configured, launched daemon."""

from __future__ import annotations

import pytest

from .test_harnessd_contract import request


@pytest.mark.parametrize(
    "daemon",
    [{"advisory_target_text": "api_token = 'top-secret'\nUse the deployment skill.\n"}],
    indirect=True,
)
def test_running_harnessd_applies_live_consent_and_revoke_without_restart(daemon):
    assert (
        request(
            daemon, "/advisory/content-bundle", payload={"target_ids": ["AGENTS.md"]}
        )[0]
        == 403
    )
    assert request(
        daemon, "/advisory/content-consent", payload={"enabled": True, "epoch": 7}
    ) == (200, {"enabled": True, "epoch": 7})
    status, bundle = request(
        daemon, "/advisory/content-bundle", payload={"target_ids": ["AGENTS.md"]}
    )
    assert status == 200
    assert bundle["targets"][0]["target_id"] == "AGENTS.md"
    assert "top-secret" not in bundle["targets"][0]["redacted_content"]
    assert request(
        daemon, "/advisory/content-consent", payload={"enabled": False, "epoch": 8}
    ) == (200, {"enabled": False, "epoch": 8})
    status, stale = request(
        daemon, "/advisory/content-consent", payload={"enabled": True, "epoch": 7}
    )
    assert (status, stale["code"], stale["current_epoch"]) == (
        409,
        "stale_consent_epoch",
        8,
    )


@pytest.mark.parametrize(
    "daemon", [{"advisory_target_text": "private prompt body\n"}], indirect=True
)
def test_harnessd_content_version_is_authenticated_hashes_only_and_no_store(daemon):
    assert request(
        daemon, "/advisory/content-consent", payload={"enabled": True, "epoch": 1}
    ) == (200, {"enabled": True, "epoch": 1})
    status, body = request(
        daemon, "/advisory/content-version", payload={"target_ids": ["AGENTS.md"]}
    )
    assert status == 200
    assert set(body) == {"bundle_hash", "targets"}
    assert set(body["targets"][0]) == {"target_id", "content_hash"}
    assert not list(daemon.root.rglob("*bundle*"))


@pytest.mark.parametrize(
    "daemon", [{"advisory_target_text": "content\n"}], indirect=True
)
def test_harnessd_content_bundle_accepts_only_configured_target_ids_and_is_ephemeral(
    daemon,
):
    assert request(
        daemon, "/advisory/content-consent", payload={"enabled": True, "epoch": 1}
    ) == (200, {"enabled": True, "epoch": 1})
    status, body = request(
        daemon,
        "/advisory/content-bundle",
        payload={"target_ids": ["UNKNOWN.md"]},
    )
    assert (status, body) == (400, {"error": "unknown advisory content target ID"})
    status, bundle = request(
        daemon, "/advisory/content-bundle", payload={"target_ids": ["AGENTS.md"]}
    )
    assert status == 200
    assert set(bundle["targets"][0]) == {
        "target_id",
        "content_hash",
        "redacted_content",
    }


@pytest.mark.parametrize(
    "daemon", [{"advisory_target_text": "content\n"}], indirect=True
)
def test_harnessd_content_bundle_rejects_oversized_request_before_parsing(daemon):
    assert request(
        daemon, "/advisory/content-consent", payload={"enabled": True, "epoch": 1}
    ) == (200, {"enabled": True, "epoch": 1})
    status, body = request(
        daemon, "/advisory/content-bundle", payload={"target_ids": ["x" * 140_000]}
    )
    assert (status, body) == (
        413,
        {"error": "content bundle request exceeds byte limit"},
    )


@pytest.mark.parametrize(
    "daemon", [{"advisory_target_text": "content\n", "no_token": True}], indirect=True
)
def test_content_consent_control_is_fail_closed_without_daemon_token(daemon):
    status, body = request(
        daemon,
        "/advisory/content-consent",
        payload={"enabled": True, "epoch": 1},
        token=False,
    )
    assert (status, body) == (401, {"error": "authentication required"})


@pytest.mark.parametrize(
    "daemon",
    [
        {
            "advisory_target_text": "content\n",
            "no_central": True,
            "preseed_consent": {"enabled": False, "epoch": 5},
        }
    ],
    indirect=True,
)
def test_content_consent_revoke_survives_process_restart(daemon):
    daemon.restart()
    status, body = request(
        daemon, "/advisory/content-consent", payload={"enabled": True, "epoch": 4}
    )
    assert (status, body["code"], body["current_epoch"], body["current_enabled"]) == (
        409,
        "stale_consent_epoch",
        5,
        False,
    )
    assert (
        request(
            daemon, "/advisory/content-bundle", payload={"target_ids": ["AGENTS.md"]}
        )[0]
        == 403
    )


@pytest.mark.parametrize(
    "daemon",
    [
        {
            "advisory_target_text": "content\n",
            "no_central": True,
            "preseed_consent": {"enabled": False, "epoch": 0},
        }
    ],
    indirect=True,
)
def test_epoch_zero_consent_is_idempotently_disabled_and_cannot_enable(daemon):
    assert request(
        daemon, "/advisory/content-consent", payload={"enabled": False, "epoch": 0}
    ) == (200, {"enabled": False, "epoch": 0})
    status, body = request(
        daemon, "/advisory/content-consent", payload={"enabled": True, "epoch": 0}
    )
    assert (status, body["error"]) == (409, "epoch zero must be disabled")
