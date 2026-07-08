"""Tests for _is_valid_title and the task-title derivation in _upsert_tasks (bug #56).

Problem: task titles were being set to raw XML fragments like
    '<observed_from_primary_session>   <what_happened>Bash</what_happened>...'

Fix: added _is_valid_title() that rejects content starting with '<' or shorter
than 10 characters.
"""

from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pytest

from drover.schema import bootstrap
from drover.server.ingest import _is_valid_title, _upsert_tasks

# ---------------------------------------------------------------------------
# Unit tests for _is_valid_title
# ---------------------------------------------------------------------------


class TestIsValidTitle:
    """Pure unit tests — no DB required."""

    def test_xml_fragment_is_rejected(self):
        xml = "<observed_from_primary_session><what_happened>Bash</what_happened></observed_from_primary_session>"
        assert _is_valid_title(xml) is False

    def test_xml_with_leading_whitespace_is_rejected(self):
        xml = "   <root><child>value</child></root>"
        assert _is_valid_title(xml) is False

    def test_short_content_under_10_chars_is_rejected(self):
        assert _is_valid_title("hi") is False
        assert _is_valid_title("short") is False
        assert _is_valid_title("123456789") is False  # exactly 9 chars

    def test_empty_string_is_rejected(self):
        assert _is_valid_title("") is False

    def test_whitespace_only_is_rejected(self):
        assert _is_valid_title("   \n  ") is False

    def test_normal_prompt_is_accepted(self):
        assert _is_valid_title("Fix the login bug in the auth module") is True

    def test_exactly_10_chars_is_accepted(self):
        assert _is_valid_title("0123456789") is True  # exactly 10 chars

    def test_multiline_prompt_is_accepted(self):
        prompt = "Refactor the database\nlayer to use async queries"
        assert _is_valid_title(prompt) is True

    def test_prompt_with_angle_bracket_not_at_start_is_accepted(self):
        """A prompt mentioning '<foo>' mid-string should still be valid."""
        assert _is_valid_title("Handle values < 10 gracefully") is True


# ---------------------------------------------------------------------------
# Integration tests for _upsert_tasks title derivation
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path):
    """Bootstrap a fresh in-memory-style DuckDB for each test."""
    parquet_dir = tmp_path / "parquet"
    db_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db_path)
    con = duckdb.connect(str(db_path))
    yield con
    con.close()


def _make_row(role: str, content: str, task_id: str = "task-abc") -> dict:
    """Build a minimal row dict that _upsert_tasks consumes."""
    return {
        "task_id": task_id,
        "repo_owner": "acme",
        "repo_name": "repo",
        "branch": "main",
        "principal_id": "user-1",
        "timestamp": datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc),
        "role": role,
        "content": content,
    }


class TestUpsertTasksTitleDerivation:
    """Integration tests that call _upsert_tasks directly against a real DB."""

    def test_xml_content_does_not_become_title(self, db):
        xml = "<observed_from_primary_session><what_happened>Bash</what_happened></observed_from_primary_session>"
        rows = [_make_row("user", xml)]
        _upsert_tasks(db, rows)
        title = db.execute(
            "SELECT title FROM tasks WHERE task_id = 'task-abc'"
        ).fetchone()[0]
        assert title is None, f"Expected None but got: {title!r}"

    def test_short_content_does_not_become_title(self, db):
        rows = [_make_row("user", "Fix it")]
        _upsert_tasks(db, rows)
        title = db.execute(
            "SELECT title FROM tasks WHERE task_id = 'task-abc'"
        ).fetchone()[0]
        assert title is None, f"Expected None but got: {title!r}"

    def test_normal_prompt_becomes_title(self, db):
        prompt = "Refactor the payment service to handle timeouts"
        rows = [_make_row("user", prompt)]
        _upsert_tasks(db, rows)
        title = db.execute(
            "SELECT title FROM tasks WHERE task_id = 'task-abc'"
        ).fetchone()[0]
        assert title == prompt

    def test_normal_prompt_is_truncated_to_120_chars(self, db):
        prompt = "A" * 200
        rows = [_make_row("user", prompt)]
        _upsert_tasks(db, rows)
        title = db.execute(
            "SELECT title FROM tasks WHERE task_id = 'task-abc'"
        ).fetchone()[0]
        assert title == "A" * 120

    def test_newlines_replaced_with_spaces_in_title(self, db):
        prompt = "Fix\nthe\nbug"
        rows = [_make_row("user", prompt)]
        _upsert_tasks(db, rows)
        title = db.execute(
            "SELECT title FROM tasks WHERE task_id = 'task-abc'"
        ).fetchone()[0]
        assert title == "Fix the bug"

    def test_skips_bad_rows_and_captures_first_valid_one(self, db):
        """Multiple rows: XML first, short second, valid third → title from third."""
        xml = "<root><item>data</item></root>"
        short = "ok"
        good = "Add retry logic to the HTTP client"

        # All rows share the same task_id so they're processed as one task.
        rows = [
            {
                **_make_row("user", xml),
                "timestamp": datetime(2026, 5, 20, 11, 0, 0, tzinfo=timezone.utc),
            },
            {
                **_make_row("user", short),
                "timestamp": datetime(2026, 5, 20, 11, 30, 0, tzinfo=timezone.utc),
            },
            {
                **_make_row("user", good),
                "timestamp": datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc),
            },
        ]
        _upsert_tasks(db, rows)
        title = db.execute(
            "SELECT title FROM tasks WHERE task_id = 'task-abc'"
        ).fetchone()[0]
        assert title == good

    def test_assistant_role_never_becomes_title(self, db):
        """Even a long assistant reply should not be used as the title."""
        rows = [
            _make_row(
                "assistant", "Here is my detailed analysis of the problem at hand."
            )
        ]
        _upsert_tasks(db, rows)
        title = db.execute(
            "SELECT title FROM tasks WHERE task_id = 'task-abc'"
        ).fetchone()[0]
        assert title is None

    def test_first_valid_user_message_wins(self, db):
        """Once a valid title is set, subsequent valid user messages don't overwrite it."""
        first = "Deploy the new inference endpoint to staging"
        second = "Now run the full regression suite against it"
        rows = [
            _make_row("user", first, task_id="task-xyz"),
            {
                **_make_row("user", second, task_id="task-xyz"),
                "timestamp": datetime(2026, 5, 20, 13, 0, 0, tzinfo=timezone.utc),
            },
        ]
        _upsert_tasks(db, rows)
        title = db.execute(
            "SELECT title FROM tasks WHERE task_id = 'task-xyz'"
        ).fetchone()[0]
        assert title == first
