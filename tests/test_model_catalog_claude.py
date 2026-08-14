from __future__ import annotations

import http.server
import json
import stat
import textwrap
import threading
import time

import pytest

from drover.server.harness.model_catalog import CatalogDiscoveryError
from drover.server.harness.model_catalog.claude import (
    ClaudeCatalogAdapter,
    ClaudeModelPolicy,
)
from drover.server.providers.claude_credentials import (
    ClaudeCredential,
    ClaudeCredentialError,
)


@pytest.fixture
def claude_credentials():
    return ClaudeCredential(
        access_token="sk-test-token",
        account_identity="account-123",
        account_label="person@example.com",
        subscription_type="max",
    )


def _page(*models, has_more=False, last_id=None):
    return json.dumps(
        {
            "data": list(models),
            "has_more": has_more,
            "first_id": models[0].get("id") if models else None,
            "last_id": last_id or (models[-1]["id"] if models else None),
        }
    ).encode()


def test_claude_catalog_applies_account_policy_without_static_aliases(
    claude_credentials, tmp_path
):
    settings = tmp_path / "settings.json"
    settings.write_text(
        '{"availableModels":["sonnet","claude-fable-5"],'
        '"modelOverrides":{"claude-fable-5":"corp/fable-prod"}}'
    )
    calls = []

    def opener(url, headers, timeout):
        calls.append((url, headers))
        return 200, _page(
            {"id": "claude-sonnet-5", "display_name": "Sonnet 5"},
            {"id": "claude-fable-5", "display_name": "Fable 5"},
            {"id": "claude-opus-5", "display_name": "Opus 5"},
        )

    adapter = ClaudeCatalogAdapter(
        command=("/usr/local/bin/claude",),
        credential_loader=lambda: claude_credentials,
        opener=opener,
        settings_paths=(settings,),
        env={},
        version_reader=lambda command: "2.1.232",
    )

    discovered = adapter.discover()

    assert [model.id for model in discovered.models] == [
        "sonnet",
        "claude-fable-5",
    ]
    assert discovered.models[1].display_name == "Fable 5"
    assert all(model.reasoning is None for model in discovered.models)
    assert calls[0][1]["Authorization"] == "Bearer sk-test-token"
    assert calls[0][1]["anthropic-beta"] == "oauth-2025-04-20"
    assert calls[0][1]["anthropic-version"] == "2023-06-01"
    assert "sk-test-token" not in repr(discovered)
    assert "sk-test-token" not in json.dumps(
        [
            {
                "id": model.id,
                "display_name": model.display_name,
                "description": model.description,
            }
            for model in discovered.models
        ]
    )


def test_claude_discovery_failure_has_no_drover_alias_fallback(
    claude_credentials,
):
    adapter = ClaudeCatalogAdapter(
        command=("claude",),
        credential_loader=lambda: claude_credentials,
        opener=lambda url, headers, timeout: (503, b'{"error":"unavailable"}'),
        settings_paths=(),
        env={},
        version_reader=lambda command: "2.1.232",
    )

    with pytest.raises(CatalogDiscoveryError, match="offline"):
        adapter.discover()


def test_policy_merges_arrays_maps_and_environment_custom_model(tmp_path):
    first = tmp_path / "first.json"
    first.write_text(
        json.dumps(
            {
                "availableModels": ["sonnet", "claude-fable-5"],
                "modelOverrides": {"claude-fable-5": "gateway/fable-old"},
            }
        )
    )
    second = tmp_path / "second.json"
    second.write_text(
        json.dumps(
            {
                "availableModels": ["sonnet", "custom/model"],
                "modelOverrides": {
                    "claude-fable-5": "gateway/fable-new",
                    "claude-sonnet-5": "gateway/sonnet",
                },
            }
        )
    )

    policy = ClaudeModelPolicy.load(
        (first, second),
        {
            "ANTHROPIC_CUSTOM_MODEL_OPTION": "custom/model",
            "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME": "Custom Model",
            "ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION": "Internal route",
        },
    )

    assert policy.available_models == (
        "sonnet",
        "claude-fable-5",
        "custom/model",
    )
    assert policy.model_overrides == {
        "claude-fable-5": "gateway/fable-new",
        "claude-sonnet-5": "gateway/sonnet",
    }
    assert policy.custom_model_id == "custom/model"
    assert policy.custom_model_name == "Custom Model"
    assert policy.custom_model_description == "Internal route"


def test_settings_environment_drives_effective_provider_and_custom_model(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "env": {
                    "ANTHROPIC_API_KEY": "settings-secret",
                    "ANTHROPIC_BASE_URL": "https://settings-gateway.example/",
                    "ANTHROPIC_CUSTOM_MODEL_OPTION": "settings/custom",
                }
            }
        )
    )
    calls = []
    adapter = ClaudeCatalogAdapter(
        command=("claude",),
        credential_loader=lambda: pytest.fail("settings API key must win"),
        opener=lambda url, headers, timeout: calls.append((url, headers))
        or (
            200,
            _page({"id": "claude-sonnet-5", "display_name": "Sonnet 5"}),
        ),
        settings_paths=(settings,),
        env={},
        version_reader=lambda command: "2.1.232",
    )

    discovered = adapter.discover()

    assert calls[0][0] == "https://settings-gateway.example/v1/models?limit=1000"
    assert calls[0][1]["x-api-key"] == "settings-secret"
    assert [model.id for model in discovered.models] == ["sonnet", "settings/custom"]


@pytest.mark.parametrize("source", ["environment", "settings"])
def test_custom_headers_configuration_is_unsupported_before_auth_or_http(
    tmp_path, source
):
    settings_paths = ()
    env = {"ANTHROPIC_CUSTOM_HEADERS": "header material"}
    if source == "settings":
        settings = tmp_path / "settings.json"
        settings.write_text(
            json.dumps({"env": {"ANTHROPIC_CUSTOM_HEADERS": "header material"}})
        )
        settings_paths = (settings,)
        env = {}
    side_effects = []
    adapter = ClaudeCatalogAdapter(
        command=("claude",),
        credential_loader=lambda: side_effects.append("credential"),
        opener=lambda url, headers, timeout: side_effects.append("http"),
        settings_paths=settings_paths,
        env=env,
        version_reader=lambda command: "2.1.232",
    )

    with pytest.raises(CatalogDiscoveryError, match="unsupported"):
        adapter.discover()

    assert side_effects == []


def test_api_key_auth_paginates_without_loading_oauth(claude_credentials):
    calls = []

    def opener(url, headers, timeout):
        calls.append((url, headers, timeout))
        if len(calls) == 1:
            return 200, _page(
                {"id": "claude-fable-5", "display_name": "Fable 5"},
                has_more=True,
                last_id="claude-fable-5",
            )
        return 200, _page({"id": "claude-sonnet-5", "display_name": "Sonnet 5"})

    adapter = ClaudeCatalogAdapter(
        command=("claude",),
        credential_loader=lambda: pytest.fail("OAuth must not be loaded"),
        opener=opener,
        settings_paths=(),
        env={
            "ANTHROPIC_API_KEY": "api-secret",
            "ANTHROPIC_BASE_URL": "https://gateway.example/",
        },
        version_reader=lambda command: "2.1.232",
    )

    discovered = adapter.discover()

    assert calls[0][0] == "https://gateway.example/v1/models?limit=1000"
    assert calls[1][0] == (
        "https://gateway.example/v1/models?limit=1000&after_id=claude-fable-5"
    )
    assert all(call[1]["x-api-key"] == "api-secret" for call in calls)
    assert all("Authorization" not in call[1] for call in calls)
    assert all(call[1]["anthropic-version"] == "2023-06-01" for call in calls)
    assert "api-secret" in discovered.account_scope_material
    assert "api-secret" not in repr(discovered)
    assert [model.id for model in discovered.models] == ["fable", "sonnet"]


def test_paginated_response_has_a_cumulative_byte_bound(claude_credentials):
    calls = 0

    def opener(url, headers, timeout):
        nonlocal calls
        calls += 1
        model_id = f"claude-family{calls}-5"
        return (
            200,
            json.dumps(
                {
                    "data": [{"id": model_id, "display_name": model_id}],
                    "has_more": calls == 1,
                    "last_id": model_id,
                    "padding": "x" * 600_000,
                }
            ).encode(),
        )

    adapter = ClaudeCatalogAdapter(
        command=("claude",),
        credential_loader=lambda: claude_credentials,
        opener=opener,
        settings_paths=(),
        env={},
        version_reader=lambda command: "2.1.232",
    )

    with pytest.raises(CatalogDiscoveryError, match="protocol_error"):
        adapter.discover()


def test_override_maps_provider_inventory_back_to_selectable_id_and_appends_custom(
    claude_credentials, tmp_path
):
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "availableModels": ["claude-fable-5", "custom/model"],
                "modelOverrides": {"claude-fable-5": "corp/fable-prod"},
            }
        )
    )
    adapter = ClaudeCatalogAdapter(
        command=("claude",),
        credential_loader=lambda: claude_credentials,
        opener=lambda url, headers, timeout: (
            200,
            _page(
                {
                    "id": "corp/fable-prod",
                    "display_name": "Corporate Fable",
                    "description": "Regional route",
                }
            ),
        ),
        settings_paths=(settings,),
        env={
            "ANTHROPIC_CUSTOM_MODEL_OPTION": "custom/model",
            "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME": "Custom Model",
            "ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION": "Internal route",
        },
        version_reader=lambda command: "2.1.232",
    )

    discovered = adapter.discover()

    assert [model.id for model in discovered.models] == [
        "claude-fable-5",
        "custom/model",
    ]
    assert discovered.models[0].display_name == "Corporate Fable"
    assert discovered.models[0].description == "Regional route"
    assert discovered.models[1].display_name == "Custom Model"
    assert discovered.models[1].description == "Internal route"


def test_custom_model_is_filtered_by_available_models(claude_credentials, tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text('{"availableModels":["sonnet"]}')
    adapter = ClaudeCatalogAdapter(
        command=("claude",),
        credential_loader=lambda: claude_credentials,
        opener=lambda url, headers, timeout: (
            200,
            _page({"id": "claude-sonnet-5", "display_name": "Sonnet 5"}),
        ),
        settings_paths=(settings,),
        env={"ANTHROPIC_CUSTOM_MODEL_OPTION": "custom/model"},
        version_reader=lambda command: "2.1.232",
    )

    discovered = adapter.discover()

    assert [model.id for model in discovered.models] == ["sonnet"]


def test_provider_pin_uses_dynamic_environment_metadata_and_allowlist(
    claude_credentials, tmp_path
):
    settings = tmp_path / "settings.json"
    settings.write_text('{"availableModels":["fable"]}')
    adapter = ClaudeCatalogAdapter(
        command=("claude",),
        credential_loader=lambda: claude_credentials,
        opener=lambda url, headers, timeout: (
            200,
            _page({"id": "claude-sonnet-5", "display_name": "Sonnet 5"}),
        ),
        settings_paths=(settings,),
        env={
            "ANTHROPIC_DEFAULT_FABLE_MODEL": "gateway/fable-v5",
            "ANTHROPIC_DEFAULT_FABLE_MODEL_NAME": "Pinned Fable",
            "ANTHROPIC_DEFAULT_FABLE_MODEL_DESCRIPTION": "Regional deployment",
            "ANTHROPIC_DEFAULT_FABLE_MODEL_SUPPORTED_CAPABILITIES": "effort",
        },
        version_reader=lambda command: "2.1.232",
    )

    discovered = adapter.discover()

    assert [model.id for model in discovered.models] == ["gateway/fable-v5"]
    assert discovered.models[0].display_name == "Pinned Fable"
    assert discovered.models[0].description == "Regional deployment"
    assert discovered.models[0].reasoning is None


def test_reasoning_requires_explicit_supported_efforts_and_default(
    claude_credentials,
):
    body = _page(
        {
            "id": "claude-explicit-5",
            "display_name": "Explicit",
            "supported_efforts": ["low", "high"],
            "default_effort": "high",
        },
        {
            "id": "claude-partial-5",
            "display_name": "Partial",
            "supported_efforts": ["low", "high"],
        },
    )
    discovered = ClaudeCatalogAdapter(
        command=("claude",),
        credential_loader=lambda: claude_credentials,
        opener=lambda url, headers, timeout: (200, body),
        settings_paths=(),
        env={},
        version_reader=lambda command: "2.1.232",
    ).discover()

    assert discovered.models[0].reasoning is not None
    assert discovered.models[0].reasoning.supported == ("low", "high")
    assert discovered.models[0].reasoning.default == "high"
    assert discovered.models[1].reasoning is None


def test_alias_is_not_invented_for_a_non_claude_provider_id(
    claude_credentials, tmp_path
):
    settings = tmp_path / "settings.json"
    settings.write_text('{"availableModels":["fable"]}')

    with pytest.raises(CatalogDiscoveryError, match="protocol_error"):
        ClaudeCatalogAdapter(
            command=("claude",),
            credential_loader=lambda: claude_credentials,
            opener=lambda url, headers, timeout: (
                200,
                _page({"id": "gateway/fable", "display_name": "Fable"}),
            ),
            settings_paths=(settings,),
            env={},
            version_reader=lambda command: "2.1.232",
        ).discover()


def test_alias_is_not_derived_from_a_model_override_key(claude_credentials, tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "availableModels": ["fable"],
                "modelOverrides": {"claude-fable-5": "corp/fable-prod"},
            }
        )
    )

    with pytest.raises(CatalogDiscoveryError, match="protocol_error"):
        ClaudeCatalogAdapter(
            command=("claude",),
            credential_loader=lambda: claude_credentials,
            opener=lambda url, headers, timeout: (
                200,
                _page(
                    {
                        "id": "corp/fable-prod",
                        "display_name": "Corporate Fable",
                    }
                ),
            ),
            settings_paths=(settings,),
            env={},
            version_reader=lambda command: "2.1.232",
        ).discover()


def test_credential_secret_does_not_affect_identity_or_repr():
    first = ClaudeCredential(
        access_token="secret-one",
        account_identity="account-123",
        account_label="person@example.com",
        subscription_type="max",
    )
    second = ClaudeCredential(
        access_token="secret-two",
        account_identity="account-123",
        account_label="person@example.com",
        subscription_type="max",
    )

    assert first == second
    assert hash(first) == hash(second)
    assert "secret-one" not in repr(first)
    assert "secret-two" not in repr(second)


def test_malformed_individual_models_are_discarded(claude_credentials):
    discovered = ClaudeCatalogAdapter(
        command=("claude",),
        credential_loader=lambda: claude_credentials,
        opener=lambda url, headers, timeout: (
            200,
            _page(
                {"type": "model", "display_name": "Missing ID"},
                {"id": "claude-invalid-5", "display_name": 42},
                {"id": "claude-sonnet-5", "display_name": "Sonnet 5"},
            ),
        ),
        settings_paths=(),
        env={},
        version_reader=lambda command: "2.1.232",
    ).discover()

    assert [model.id for model in discovered.models] == ["sonnet"]


@pytest.mark.parametrize(
    ("failure", "category"),
    [
        (lambda: (401, b"{}"), "not_authenticated"),
        (lambda: (403, b"{}"), "not_authenticated"),
        (lambda: (302, b""), "offline"),
        (lambda: (500, b"{}"), "offline"),
        (lambda: (_ for _ in ()).throw(TimeoutError()), "timeout"),
        (lambda: (_ for _ in ()).throw(OSError()), "offline"),
        (lambda: (200, b"not-json"), "protocol_error"),
        (lambda: (200, b"x" * (1024 * 1024 + 1)), "protocol_error"),
    ],
)
def test_claude_catalog_failures_use_safe_categories(
    claude_credentials, failure, category
):
    adapter = ClaudeCatalogAdapter(
        command=("claude",),
        credential_loader=lambda: claude_credentials,
        opener=lambda url, headers, timeout: failure(),
        settings_paths=(),
        env={},
        version_reader=lambda command: "2.1.232",
    )

    with pytest.raises(CatalogDiscoveryError, match=category):
        adapter.discover()


def test_credential_failure_is_safely_mapped():
    for credential_error, expected in (
        (
            ClaudeCredentialError("not_authenticated", status="usage_unavailable"),
            "not_authenticated",
        ),
        (
            ClaudeCredentialError("token_expired", status="usage_unavailable"),
            "not_authenticated",
        ),
        (ClaudeCredentialError("protocol_error", status="error"), "protocol_error"),
    ):
        adapter = ClaudeCatalogAdapter(
            command=("claude",),
            credential_loader=lambda error=credential_error: (_ for _ in ()).throw(
                error
            ),
            opener=lambda url, headers, timeout: pytest.fail("must not call HTTP"),
            settings_paths=(),
            env={},
            version_reader=lambda command: "2.1.232",
        )
        with pytest.raises(CatalogDiscoveryError, match=expected):
            adapter.discover()


def test_empty_version_is_a_protocol_error(claude_credentials):
    adapter = ClaudeCatalogAdapter(
        command=("claude",),
        credential_loader=lambda: claude_credentials,
        opener=lambda url, headers, timeout: (
            200,
            _page({"id": "claude-sonnet-5", "display_name": "Sonnet 5"}),
        ),
        settings_paths=(),
        env={},
        version_reader=lambda command: "",
    )

    with pytest.raises(CatalogDiscoveryError, match="protocol_error"):
        adapter.discover()


def test_version_reader_terminates_at_output_bound(claude_credentials, tmp_path):
    executable = tmp_path / "claude"
    executable.write_text(textwrap.dedent("""
            #!/usr/bin/env python3
            while True:
                print("x" * 65536, flush=True)
            """).lstrip())
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    adapter = ClaudeCatalogAdapter(
        command=(str(executable),),
        credential_loader=lambda: claude_credentials,
        opener=lambda url, headers, timeout: (
            200,
            _page({"id": "claude-sonnet-5", "display_name": "Sonnet 5"}),
        ),
        settings_paths=(),
        env={},
        timeout_s=2,
    )
    started = time.monotonic()

    with pytest.raises(CatalogDiscoveryError, match="protocol_error"):
        adapter.discover()

    assert time.monotonic() - started < 1


def test_cache_identity_tracks_configuration_but_not_api_key(tmp_path):
    executable = tmp_path / "claude"
    executable.write_text("first")
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    account = tmp_path / ".claude.json"
    account.write_text("{}")
    credentials = tmp_path / ".credentials.json"
    credentials.write_text("{}")
    common = dict(
        command=(str(executable),),
        credential_loader=lambda: pytest.fail("not used"),
        opener=lambda url, headers, timeout: pytest.fail("not used"),
        settings_paths=(settings,),
        account_path=account,
        credentials_path=credentials,
        version_reader=lambda command: "2.1.232",
    )
    first = ClaudeCatalogAdapter(
        **common,
        env={
            "ANTHROPIC_API_KEY": "first-secret",
            "ANTHROPIC_CUSTOM_MODEL_OPTION": "custom/first",
        },
    )
    second = ClaudeCatalogAdapter(
        **common,
        env={
            "ANTHROPIC_API_KEY": "second-secret",
            "ANTHROPIC_CUSTOM_MODEL_OPTION": "custom/first",
        },
    )
    changed = ClaudeCatalogAdapter(
        **common,
        env={
            "ANTHROPIC_API_KEY": "second-secret",
            "ANTHROPIC_CUSTOM_MODEL_OPTION": "custom/second",
        },
    )

    assert first.cache_identity() == second.cache_identity()
    assert second.cache_identity() != changed.cache_identity()
    before = first.cache_identity()
    settings.write_text('{"availableModels":["sonnet"]}')
    assert first.cache_identity() != before


def test_cache_identity_ignores_unrelated_model_named_environment_values(tmp_path):
    common = dict(
        command=(str(tmp_path / "claude"),),
        credential_loader=lambda: pytest.fail("not used"),
        opener=lambda url, headers, timeout: pytest.fail("not used"),
        settings_paths=(),
        version_reader=lambda command: "2.1.232",
    )

    first = ClaudeCatalogAdapter(**common, env={"PRIVATE_MODEL_NAME": "secret-one"})
    second = ClaudeCatalogAdapter(**common, env={"PRIVATE_MODEL_NAME": "secret-two"})

    assert first.cache_identity() == second.cache_identity()


def test_cache_identity_tracks_custom_header_presence_without_its_value(tmp_path):
    common = dict(
        command=(str(tmp_path / "claude"),),
        credential_loader=lambda: pytest.fail("not used"),
        opener=lambda url, headers, timeout: pytest.fail("not used"),
        settings_paths=(),
        version_reader=lambda command: "2.1.232",
    )
    absent = ClaudeCatalogAdapter(**common, env={})
    first = ClaudeCatalogAdapter(
        **common, env={"ANTHROPIC_CUSTOM_HEADERS": "secret-one"}
    )
    second = ClaudeCatalogAdapter(
        **common, env={"ANTHROPIC_CUSTOM_HEADERS": "secret-two"}
    )

    assert absent.cache_identity() != first.cache_identity()
    assert first.cache_identity() == second.cache_identity()


def test_non_anthropic_provider_path_fails_without_using_wrong_credentials():
    adapter = ClaudeCatalogAdapter(
        command=("claude",),
        credential_loader=lambda: pytest.fail("must not load Anthropic OAuth"),
        opener=lambda url, headers, timeout: pytest.fail("must not call Anthropic"),
        settings_paths=(),
        env={"CLAUDE_CODE_USE_BEDROCK": "1"},
        version_reader=lambda command: "2.1.232",
    )

    with pytest.raises(CatalogDiscoveryError, match="unsupported"):
        adapter.discover()


def test_default_opener_refuses_redirect_without_leaking_api_key():
    leaked_keys: list[str | None] = []

    class Target(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            leaked_keys.append(self.headers.get("x-api-key"))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    target = http.server.HTTPServer(("127.0.0.1", 0), Target)
    target_thread = threading.Thread(target=target.serve_forever, daemon=True)
    target_thread.start()

    class Origin(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header(
                "Location", f"http://127.0.0.1:{target.server_port}/collect"
            )
            self.end_headers()

        def log_message(self, *args):
            pass

    origin = http.server.HTTPServer(("127.0.0.1", 0), Origin)
    origin_thread = threading.Thread(target=origin.serve_forever, daemon=True)
    origin_thread.start()
    try:
        adapter = ClaudeCatalogAdapter(
            command=("claude",),
            credential_loader=lambda: pytest.fail("must use API key"),
            settings_paths=(),
            env={
                "ANTHROPIC_API_KEY": "api-secret",
                "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{origin.server_port}",
            },
            version_reader=lambda command: "2.1.232",
        )
        with pytest.raises(CatalogDiscoveryError, match="offline"):
            adapter.discover()
    finally:
        origin.shutdown()
        origin_thread.join()
        target.shutdown()
        target_thread.join()

    assert leaked_keys == []
