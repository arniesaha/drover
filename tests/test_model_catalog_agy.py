import stat
import textwrap
import time

import pytest

from drover.server.harness.model_catalog import CatalogDiscoveryError
from drover.server.harness.model_catalog.agy import AgyCatalogAdapter


@pytest.fixture
def fake_agy(tmp_path):
    executable = tmp_path / "agy"
    executable.write_text(textwrap.dedent("""
            #!/usr/bin/env python3
            import sys
            import time

            mode = sys.argv[1] if len(sys.argv) > 1 else "normal"
            if "--version" in sys.argv:
                if mode == "empty-version":
                    raise SystemExit(0)
                if mode == "version-fail":
                    raise SystemExit(3)
                print("agy 0.9.4")
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
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable


def test_agy_catalog_uses_native_ids_and_omits_separate_reasoning(fake_agy, tmp_path):
    accounts = tmp_path / "google_accounts.json"
    accounts.write_text('{"active":"person@example.com"}')
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
