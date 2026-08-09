"""Privacy and containment tests for opt-in advisory content bundles."""

from pathlib import Path

import pytest

from drover.server.advisory.content_targets import (
    ContentTarget,
    ContentTargetError,
    build_content_bundle,
)
from drover.server.advisory.redaction import redact_content


def _target(path: Path, target_id: str = "test-target") -> ContentTarget:
    return ContentTarget(path=path, target_id=target_id)


def test_bundle_rejects_symlink_escape(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    secret = tmp_path / "secret"
    secret.write_text("do not read")
    escape = allowed / "escape"
    escape.symlink_to(secret)

    with pytest.raises(ContentTargetError, match="outside allowlist"):
        build_content_bundle([_target(escape)], allowed_roots=[allowed])


def test_bundle_rejects_symlink_even_when_it_stays_in_allowlist(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    source = allowed / "source.txt"
    source.write_text("safe")
    alias = allowed / "alias.txt"
    alias.symlink_to(source)

    with pytest.raises(ContentTargetError, match="symlink"):
        build_content_bundle([_target(alias)], allowed_roots=[allowed])


def test_bundle_rejects_parent_traversal_syntax(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    source = allowed / "source.txt"
    source.write_text("safe")
    traversing = allowed / "nested" / ".." / "source.txt"

    with pytest.raises(ContentTargetError, match="traversal"):
        build_content_bundle([_target(traversing)], allowed_roots=[allowed])


def test_bundle_rejects_paths_outside_allowlist_and_nonregular_targets(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")

    with pytest.raises(ContentTargetError, match="outside allowlist"):
        build_content_bundle([_target(outside)], allowed_roots=[allowed])
    with pytest.raises(ContentTargetError, match="regular file"):
        build_content_bundle([_target(allowed)], allowed_roots=[allowed])


def test_bundle_rejects_oversized_file_without_modifying_it(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    source = allowed / "prompt.txt"
    original = "123456789"
    source.write_text(original)

    with pytest.raises(ContentTargetError, match="per-file byte limit"):
        build_content_bundle(
            [_target(source)], allowed_roots=[allowed], max_file_bytes=8
        )

    assert source.read_text() == original


def test_bundle_aborts_when_aggregate_input_exceeds_limit(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    first = allowed / "first.txt"
    second = allowed / "second.txt"
    first.write_text("12345")
    second.write_text("67890")

    with pytest.raises(ContentTargetError, match="aggregate byte limit"):
        build_content_bundle(
            [_target(first, "first"), _target(second, "second")],
            allowed_roots=[allowed],
            max_bundle_bytes=9,
        )


def test_bundle_rejects_invalid_utf8(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    source = allowed / "prompt.txt"
    source.write_bytes(b"valid-prefix\xffsecret")

    with pytest.raises(ContentTargetError, match="UTF-8"):
        build_content_bundle([_target(source)], allowed_roots=[allowed])


def test_redaction_covers_structured_credentials_and_token_patterns():
    content = """{
  "api_key": "sk-live-json-secret",
  "nested": {"password": "hunter2"},
  "ordinary": "keep me"
}
token = "toml-secret"
Authorization: Bearer bearer-secret
github_token=ghp_abcdefghijklmnopqrstuvwxyz123456
JWT eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signaturevalue
"""

    redacted = redact_content(content)

    for secret in (
        "sk-live-json-secret",
        "hunter2",
        "toml-secret",
        "bearer-secret",
        "ghp_abcdefghijklmnopqrstuvwxyz123456",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signaturevalue",
    ):
        assert secret not in redacted
    assert "keep me" in redacted
    assert "[REDACTED]" in redacted


def test_redaction_covers_inline_and_multiline_toml_credentials():
    content = '''service = { token = "inline-secret", name = "keep-me" }
password = """multi-line
secret-value
"""
'''

    redacted = redact_content(content)

    assert "inline-secret" not in redacted
    assert "multi-line" not in redacted
    assert "secret-value" not in redacted
    assert "keep-me" in redacted


def test_hashes_are_derived_only_from_redacted_content(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    source = allowed / "config.toml"
    source.write_text('name = "drover"\napi_key = "first-secret"\n')

    first = build_content_bundle([_target(source)], allowed_roots=[allowed])
    source.write_text('name = "drover"\napi_key = "second-secret"\n')
    second = build_content_bundle([_target(source)], allowed_roots=[allowed])

    assert first.targets[0].redacted_content == second.targets[0].redacted_content
    assert first.targets[0].content_hash == second.targets[0].content_hash
    assert first.bundle_hash == second.bundle_hash
    assert "second-secret" not in second.targets[0].redacted_content


def test_bundle_preserves_allowed_redacted_content_and_metadata(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    source = allowed / "AGENTS.md"
    source.write_text("Use evidence before claims.\n")

    bundle = build_content_bundle(
        [_target(source, "global-agents")],
        allowed_roots=[allowed],
        host_id="mac-mini",
    )

    assert bundle.host_id == "mac-mini"
    assert bundle.created_at.tzinfo is not None
    assert len(bundle.bundle_hash) == 64
    assert len(bundle.targets) == 1
    assert bundle.targets[0].target_id == "global-agents"
    assert bundle.targets[0].redacted_content == "Use evidence before claims.\n"
    assert len(bundle.targets[0].content_hash) == 64
