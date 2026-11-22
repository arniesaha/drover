from __future__ import annotations

import stat
import textwrap

import pytest

from drover.server.harness.model_catalog.cursor import CursorCatalogAdapter
from drover.server.harness.model_catalog.service import CatalogDiscoveryError


@pytest.fixture
def fake_cursor(tmp_path):
    executable = tmp_path / "cursor-agent"
    executable.write_text(textwrap.dedent("""
            #!/usr/bin/env python3
            import sys
            mode = sys.argv[1] if len(sys.argv) > 1 else "normal"
            if "--version" in sys.argv:
                print("2026.11.19-f005e9d")
                raise SystemExit(0)
            if "status" in sys.argv:
                print('{"isAuthenticated": ' + ("false" if mode == "signed-out" else "true") + "}")
                raise SystemExit(0)
            if "models" in sys.argv:
                if mode.startswith("listing-"):
                    count = int(mode.removeprefix("listing-"))
                    rows = [f"public-model-{index:03d} - Public Model {index}" for index in range(count)]
                    rows[143] += " (current, default)"
                    print("Available models\\n\\n" + "\\n".join(rows) + "\\n\\nTip: use --model <id>")
                    raise SystemExit(0)
                if mode == "malformed":
                    print("Available models\\n\\nvalid-model - Valid Model (default)\\nbad model - invalid\\n\\nTip: use --model")
                    raise SystemExit(0)
                if mode == "failed":
                    raise SystemExit(2)
                print("\\x1b[2mAvailable models\\x1b[0m\\n\\n\\x1b[36mgrok-4.7\\x1b[0m - Grok 4.7 (current, default)\\ncomposer-2.5 - Composer 2.5\\n\\x1b[2mTip: use --model <id>\\x1b[0m")
                raise SystemExit(0)
            raise SystemExit(3)
            """).lstrip())
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable


def test_cursor_catalog_uses_only_ids_listed_by_the_authenticated_cli(fake_cursor):
    catalog = CursorCatalogAdapter((str(fake_cursor),), timeout_s=1).discover()

    assert [model.id for model in catalog.models] == ["grok-4.7", "composer-2.5"]
    assert [model.display_name for model in catalog.models] == [
        "Grok 4.7",
        "Composer 2.5",
    ]
    assert catalog.models[0].is_default is True
    assert catalog.harness_version == "2026.11.19-f005e9d"
    assert catalog.account_scope_material == "cursor|authenticated"


def test_cursor_catalog_requires_authenticated_status_before_models(fake_cursor):
    with pytest.raises(CatalogDiscoveryError, match="not_authenticated"):
        CursorCatalogAdapter((str(fake_cursor), "signed-out"), timeout_s=1).discover()


def test_cursor_catalog_ignores_malformed_rows_but_requires_real_model_rows(
    fake_cursor,
):
    catalog = CursorCatalogAdapter(
        (str(fake_cursor), "malformed"), timeout_s=1
    ).discover()

    assert [model.id for model in catalog.models] == ["valid-model"]


def test_cursor_catalog_nonzero_model_command_is_not_a_fake_catalog(fake_cursor):
    with pytest.raises(CatalogDiscoveryError, match="protocol_error"):
        CursorCatalogAdapter((str(fake_cursor), "failed"), timeout_s=1).discover()


def test_cursor_catalog_preserves_a_current_sized_listing_and_default(fake_cursor):
    catalog = CursorCatalogAdapter(
        (str(fake_cursor), "listing-284"), timeout_s=1
    ).discover()

    assert len(catalog.models) == 284
    assert catalog.models[0].id == "public-model-000"
    assert catalog.models[-1].id == "public-model-283"
    assert catalog.models[143].is_default is True


def test_cursor_catalog_rejects_listings_beyond_the_shared_bound(fake_cursor):
    with pytest.raises(CatalogDiscoveryError, match="protocol_error"):
        CursorCatalogAdapter((str(fake_cursor), "listing-513"), timeout_s=1).discover()
