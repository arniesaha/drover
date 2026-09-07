"""Tests for the authenticated staging release identity endpoint."""

from __future__ import annotations

import json
import pytest

from drover.server.release_identity import load_release_identity


def _environ(**extra: str) -> dict[str, str]:
    return {
        "DROVER_RELEASE_ROLE": "testflight-staging",
        "DROVER_RELEASE_SHA": "a" * 40,
        **extra,
    }


def test_load_release_identity_exposes_only_the_safe_contract():
    identity = load_release_identity(_environ())

    assert identity.role == "testflight-staging"
    assert identity.source_sha == "a" * 40
    assert identity.staging_probe is None
    assert set(identity.as_json()) == {
        "package_version",
        "role",
        "source_sha",
        "staging_probe",
    }


@pytest.mark.parametrize(
    "environ",
    (
        {},
        {"DROVER_RELEASE_ROLE": "production", "DROVER_RELEASE_SHA": "a" * 40},
        {"DROVER_RELEASE_ROLE": "testflight-staging", "DROVER_RELEASE_SHA": "A" * 40},
        {
            "DROVER_RELEASE_ROLE": "testflight-staging",
            "DROVER_RELEASE_SHA": "not-a-sha",
        },
    ),
)
def test_load_release_identity_rejects_invalid_launch_values_safely(environ):
    with pytest.raises(ValueError, match="^invalid staging release identity$"):
        load_release_identity(environ)


def test_load_release_identity_includes_a_matching_owner_only_probe(tmp_path):
    attestation = tmp_path / "probe.json"
    attestation.write_text(
        json.dumps(
            {
                "source_sha": "a" * 40,
                "host_id": "testflight-staging-mac-mini",
                "completed_at": "2026-09-07T00:00:00+00:00",
                "session_id_sha256": "b" * 64,
            }
        ),
        encoding="utf-8",
    )
    attestation.chmod(0o600)

    identity = load_release_identity(
        _environ(DROVER_STAGING_ATTESTATION_PATH=str(attestation))
    )

    assert identity.staging_probe is not None
    assert identity.staging_probe.as_json() == json.loads(attestation.read_text())


@pytest.mark.parametrize(
    "payload, mode",
    [
        ({"nope": True}, 0o600),
        ({"source_sha": "b" * 40}, 0o600),
        ({"source_sha": "a" * 40}, 0o640),
        (
            {
                "source_sha": "a" * 40,
                "host_id": "testflight-staging-mac-mini",
                "completed_at": "not-a-timestamp",
                "session_id_sha256": "b" * 64,
            },
            0o600,
        ),
    ],
)
def test_invalid_or_non_owner_only_probe_is_absent(tmp_path, payload, mode):
    attestation = tmp_path / "probe.json"
    attestation.write_text(json.dumps(payload), encoding="utf-8")
    attestation.chmod(mode)

    identity = load_release_identity(
        _environ(DROVER_STAGING_ATTESTATION_PATH=str(attestation))
    )

    assert identity.staging_probe is None
