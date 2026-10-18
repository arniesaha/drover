"""Small deterministic values that a Rust implementation must reproduce."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from drover.server.harness.folders import FolderError, list_folders
from drover.server.harness.identity import harness_event_identity
from drover.server.harness.model_catalog.scope import AccountScopeIDs

VECTORS = json.loads((Path(__file__).parent / "golden_vectors.json").read_text())


def test_event_identity_golden_is_byte_compatible():
    actual = harness_event_identity(
        session_id="s-1",
        seq=7,
        event_type="tool_result",
        created_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        payload={
            "event_id": "new",
            "aggregated_output": "drop",
            "nested": {"event_id": "nested", "ok": True},
        },
    )
    assert actual == VECTORS["event_identity"]


def test_model_catalog_scope_hmac_golden():
    assert (
        AccountScopeIDs(secret=b"x" * 32).for_material("account@example.test")
        == VECTORS["account_scope_id"]
    )


def test_folder_cursor_is_bound_to_the_directory_snapshot(monkeypatch, tmp_path):
    root = tmp_path / "home"
    root.mkdir()
    (root / "a").mkdir()
    (root / "b").mkdir()
    monkeypatch.setattr("drover.server.harness.folders.allowed_roots", lambda: (root,))
    first = list_folders(str(root), limit=1)
    assert first["next_cursor"]
    (root / "changed").mkdir()
    with pytest.raises(FolderError, match="invalid_cursor"):
        list_folders(str(root), cursor=first["next_cursor"], limit=1)
