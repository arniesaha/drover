import base64
import json
import stat
import textwrap
import time
from pathlib import Path

import pytest

from drover.server.harness.model_catalog import (
    AccountScopeIDs,
    CatalogDiscoveryError,
    ModelCatalogService,
)
from drover.server.harness.model_catalog.agy import AgyCatalogAdapter


@pytest.fixture(autouse=True)
def isolated_keychain(monkeypatch):
    monkeypatch.setattr("drover.server.providers.agy._read_keychain", lambda: None)


@pytest.fixture
def fake_agy(tmp_path):
    executable = tmp_path / "agy"
    executable.write_text(textwrap.dedent("""
            #!/usr/bin/env python3
            import sys
            from pathlib import Path
            import time

            mode = sys.argv[1] if len(sys.argv) > 1 else "normal"
            if "--version" in sys.argv:
                if mode == "empty-version":
                    raise SystemExit(0)
                if mode == "version-fail":
                    raise SystemExit(3)
                print("1.2.11" if mode == "current" else "agy 0.9.4")
                raise SystemExit(0)
            if mode == "current":
                print(Path(__file__).with_name("models-1.2.11.tsv").read_text(), end="")
                raise SystemExit(0)
            if "models" not in sys.argv:
                raise SystemExit(4)
            if mode == "timeout":
                time.sleep(10)
            if mode == "nonzero":
                print("Fetching available models...")
                raise SystemExit(2)
            if mode == "oversized":
                print("x" * (256 * 1024 + 1))
                raise SystemExit(0)
            if mode == "overproduce":
                while True:
                    print("x" * 65536, flush=True)
            if mode == "whitespace":
                print(" model-with-space \\t Display Name with space ")
                raise SystemExit(0)
            if mode == "malformed":
                print("Fetching available models...")
                print("missing-name")
                print("too-many\\tfields\\textra")
                print("\\tno-id")
                print("valid-model\\tValid model")
                raise SystemExit(0)
            print("Fetching available models...")
            print("gemini-3.7-flash-high\\tGemini 3.7 Flash High")
            print("gemini-3.7-flash-medium\\tGemini 3.7 Flash Medium")
            print("gemini-3.7-flash-high\\tDuplicate ignored")
            print("claude-sonnet-4-6\\tClaude Sonnet 4.6")
            raise SystemExit(0)
            """).lstrip())
    executable.with_name("models-1.2.11.tsv").write_text(
        (Path(__file__).parent / "fixtures/agy/models-1.2.11.tsv").read_text()
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable


def test_agy_catalog_uses_native_ids_and_omits_separate_reasoning(fake_agy, tmp_path):
    accounts = tmp_path / "google_accounts.json"
    accounts.write_text(
        (Path(__file__).parent / "fixtures/agy/legacy-accounts.json").read_text()
    )
    adapter = AgyCatalogAdapter(
        command=(str(fake_agy),), accounts_path=accounts, timeout_s=1
    )

    discovered = adapter.discover()

    assert [model.id for model in discovered.models] == [
        "gemini-3.7-flash-high",
        "gemini-3.7-flash-medium",
        "claude-sonnet-4-6",
    ]
    assert [model.display_name for model in discovered.models] == [
        "Gemini 3.7 Flash High",
        "Gemini 3.7 Flash Medium",
        "Claude Sonnet 4.6",
    ]
    assert all(model.reasoning is None for model in discovered.models)
    assert discovered.account_scope_material == "agy|person@example.com"
    assert discovered.harness_version == "agy 0.9.4"


def test_agy_catalog_ignores_malformed_rows_and_requires_valid_rows(fake_agy, tmp_path):
    accounts = tmp_path / "google_accounts.json"
    accounts.write_text('{"active":"person@example.com"}')
    adapter = AgyCatalogAdapter(
        (str(fake_agy), "malformed"), accounts_path=accounts, timeout_s=1
    )

    discovered = adapter.discover()

    assert [model.id for model in discovered.models] == ["valid-model"]


@pytest.mark.parametrize("mode", ["oversized", "nonzero"])
def test_agy_catalog_process_failures_are_protocol_errors(fake_agy, tmp_path, mode):
    accounts = tmp_path / "google_accounts.json"
    accounts.write_text('{"active":"person@example.com"}')
    with pytest.raises(CatalogDiscoveryError, match="protocol_error"):
        AgyCatalogAdapter(
            (str(fake_agy), mode), accounts_path=accounts, timeout_s=1
        ).discover()


def test_agy_catalog_terminates_an_overproducing_process_at_the_output_bound(
    fake_agy, tmp_path
):
    accounts = tmp_path / "google_accounts.json"
    accounts.write_text('{"active":"person@example.com"}')
    started = time.monotonic()

    with pytest.raises(CatalogDiscoveryError, match="protocol_error"):
        AgyCatalogAdapter(
            (str(fake_agy), "overproduce"), accounts_path=accounts, timeout_s=2
        ).discover()

    assert time.monotonic() - started < 1


def test_agy_catalog_preserves_non_empty_native_field_whitespace(fake_agy, tmp_path):
    accounts = tmp_path / "google_accounts.json"
    accounts.write_text('{"active":"person@example.com"}')

    discovered = AgyCatalogAdapter(
        (str(fake_agy), "whitespace"), accounts_path=accounts
    ).discover()

    assert discovered.models[0].id == " model-with-space "
    assert discovered.models[0].display_name == " Display Name with space "


def test_agy_catalog_missing_executable_is_unsupported(tmp_path):
    accounts = tmp_path / "google_accounts.json"
    accounts.write_text('{"active":"person@example.com"}')

    with pytest.raises(CatalogDiscoveryError, match="unsupported"):
        AgyCatalogAdapter(
            (str(tmp_path / "gone"),), accounts_path=accounts, timeout_s=0.05
        ).discover()


def test_agy_catalog_missing_executable_precedes_missing_account(tmp_path):
    with pytest.raises(CatalogDiscoveryError, match="unsupported"):
        AgyCatalogAdapter(
            (str(tmp_path / "gone"),),
            accounts_path=tmp_path / "missing-accounts.json",
            timeout_s=0.05,
        ).discover()


def test_agy_catalog_missing_account_precedes_present_nonzero_executable(
    fake_agy, tmp_path
):
    with pytest.raises(CatalogDiscoveryError, match="not_authenticated"):
        AgyCatalogAdapter(
            (str(fake_agy), "nonzero"),
            accounts_path=tmp_path / "missing-accounts.json",
            timeout_s=1,
        ).discover()


def test_agy_catalog_missing_account_precedes_present_timeout_executable(
    fake_agy, tmp_path
):
    with pytest.raises(CatalogDiscoveryError, match="not_authenticated"):
        AgyCatalogAdapter(
            (str(fake_agy), "timeout"),
            accounts_path=tmp_path / "missing-accounts.json",
            timeout_s=0.01,
        ).discover()


def test_agy_catalog_timeout_is_safe_failure(fake_agy, tmp_path):
    accounts = tmp_path / "google_accounts.json"
    accounts.write_text('{"active":"person@example.com"}')

    with pytest.raises(CatalogDiscoveryError, match="timeout"):
        AgyCatalogAdapter(
            (str(fake_agy), "timeout"), accounts_path=accounts, timeout_s=0.01
        ).discover()


def test_agy_catalog_requires_active_or_old_account(fake_agy, tmp_path):
    accounts = tmp_path / "google_accounts.json"
    accounts.write_text('{"active":null,"old":["", " "]}')

    with pytest.raises(CatalogDiscoveryError, match="not_authenticated"):
        AgyCatalogAdapter((str(fake_agy),), accounts_path=accounts).discover()

    accounts.write_text('{"active":null,"old":["old@example.com"]}')
    discovered = AgyCatalogAdapter((str(fake_agy),), accounts_path=accounts).discover()
    assert discovered.account_scope_material == "agy|old@example.com"


def test_agy_catalog_rejects_version_failure(fake_agy, tmp_path):
    accounts = tmp_path / "google_accounts.json"
    accounts.write_text('{"active":"person@example.com"}')

    with pytest.raises(CatalogDiscoveryError, match="protocol_error"):
        AgyCatalogAdapter(
            (str(fake_agy), "version-fail"), accounts_path=accounts
        ).discover()

    with pytest.raises(CatalogDiscoveryError, match="protocol_error"):
        AgyCatalogAdapter(
            (str(fake_agy), "empty-version"), accounts_path=accounts
        ).discover()


def test_agy_catalog_cache_identity_tracks_command_and_accounts_stat(
    fake_agy, tmp_path
):
    accounts = tmp_path / "google_accounts.json"
    accounts.write_text('{"active":"person@example.com"}')
    adapter = AgyCatalogAdapter((str(fake_agy),), accounts_path=accounts)

    before = adapter.cache_identity()
    accounts.write_text('{"active":"other@example.com"}')
    after = adapter.cache_identity()

    assert before != after


@pytest.mark.parametrize("store", ["file", "keychain", "wrapped-keychain"])
def test_current_agy_catalog_without_accounts_file(fake_agy, tmp_path, store, caplog):
    credential = (
        Path(__file__).parent / "fixtures/agy/identity-present.json"
    ).read_text()
    token_file = tmp_path / "antigravity-cli/antigravity-oauth-token"
    if store == "file":
        token_file.parent.mkdir()
        token_file.write_text(credential)
    raw = credential
    if store == "wrapped-keychain":
        raw = "go-keyring-base64:" + base64.b64encode(raw.encode()).decode()
    adapter = AgyCatalogAdapter(
        (str(fake_agy), "current"),
        accounts_path=tmp_path / "google_accounts.json",
        keychain_reader=lambda: raw if store != "file" else None,
    )
    service = ModelCatalogService(
        host_id="studio",
        adapters={"agy": adapter},
        scope_ids=AccountScopeIDs(secret=b"x" * 32),
    )

    envelope = service.read("agy")

    assert not envelope.stale
    assert envelope.stale_reason is None
    assert envelope.discovered_at is not None
    assert envelope.harness_version == "1.2.11"
    assert len(envelope.models) == 14
    assert envelope.models[0].id == "gemini-3.8-flash-high"
    assert envelope.models[-1].id == "gpt-oss-120b-medium"
    assert envelope.account_scope_id
    wire = json.dumps(envelope.to_wire())
    assert "@" not in wire + caplog.text
    assert credential not in wire + caplog.text
    assert "fixture-token" not in wire + caplog.text
    assert "@" not in repr(adapter.discover())


def test_current_credential_precedes_legacy_and_invalidates_cache(fake_agy, tmp_path):
    accounts = tmp_path / "google_accounts.json"
    accounts.write_text('{"active":"legacy@example.com"}')
    credential = (
        Path(__file__).parent / "fixtures/agy/identity-present.json"
    ).read_text()
    raw = credential
    adapter = AgyCatalogAdapter(
        (str(fake_agy),),
        accounts_path=accounts,
        keychain_reader=lambda: raw,
    )
    service = ModelCatalogService(
        host_id="studio",
        adapters={"agy": adapter},
        scope_ids=AccountScopeIDs(secret=b"x" * 32),
    )
    first = service.read("agy")
    initial_identity = adapter.cache_identity()
    raw = None
    accounts.unlink()
    assert adapter.cache_identity() != initial_identity
    signed_out = service.read("agy")
    assert signed_out.stale_reason == "not_authenticated"
    assert signed_out.discovered_at == first.discovered_at
    raw = json.dumps({"token": {"access_token": "different-fixture-token"}})
    switched = service.read("agy")
    assert not switched.stale
    assert switched.account_scope_id != first.account_scope_id


@pytest.mark.parametrize("id_token", [None, "header.!invalid!.signature"])
def test_credential_without_identity_still_discovers_models(
    fake_agy, tmp_path, id_token
):
    credential = json.dumps(
        {"token": {"access_token": "fixture-token"}, "id_token": id_token}
    )
    discovered = AgyCatalogAdapter(
        (str(fake_agy),),
        accounts_path=tmp_path / "missing.json",
        keychain_reader=lambda: credential,
    ).discover()
    assert discovered.models
    assert "fixture-token" not in discovered.account_scope_material


@pytest.mark.parametrize(
    "credential", ["{}", '{"id_token":"header.payload.signature"}', "invalid"]
)
def test_id_claims_or_malformed_credential_alone_are_not_sign_in(
    fake_agy, tmp_path, credential
):
    with pytest.raises(CatalogDiscoveryError, match="not_authenticated"):
        AgyCatalogAdapter(
            (str(fake_agy),),
            accounts_path=tmp_path / "missing.json",
            keychain_reader=lambda: credential,
        ).discover()
