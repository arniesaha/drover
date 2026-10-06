"""Tests for the deterministic summarizer derivations."""

from __future__ import annotations

import json

import duckdb

from drover.server.summarizer.derive import compute_files_touched, compute_tools_used
from drover.server.summarizer.worker import _tool_projection_sql


def _ev(raw_data: dict) -> dict:
    return {"raw_data": json.dumps(raw_data)}


def test_files_touched_from_edit_blocks() -> None:
    events = [
        _ev(
            {
                "tool_use_blocks": [
                    {"name": "Edit", "input": {"file_path": "src/foo.py"}},
                    {"name": "Write", "input": {"file_path": "src/bar.py"}},
                ]
            }
        ),
        _ev(
            {
                "tool_use_blocks": [
                    {"name": "Edit", "input": {"file_path": "src/foo.py"}},  # dup
                ]
            }
        ),
    ]
    files = compute_files_touched(events)
    assert files == ["src/bar.py", "src/foo.py"]


def test_files_touched_path_aliases() -> None:
    events = [
        _ev(
            {
                "tool_use_blocks": [
                    {"name": "Read", "input": {"path": "docs/spec.md"}},
                ]
            }
        ),
    ]
    assert compute_files_touched(events) == ["docs/spec.md"]


def test_files_touched_skips_malformed() -> None:
    events = [
        {"raw_data": "not json"},
        {"raw_data": ""},
        {"raw_data": json.dumps({"tool_use_blocks": "not a list"})},
        {"raw_data": json.dumps({"tool_use_blocks": [{"name": "Edit"}]})},  # no input
    ]
    assert compute_files_touched(events) == []


def test_tools_used_counter() -> None:
    events = [
        _ev(
            {
                "tool_use_blocks": [
                    {"name": "Edit", "input": {}},
                    {"name": "Edit", "input": {}},
                    {"name": "Bash", "input": {}},
                ]
            }
        ),
        _ev(
            {
                "tool_use_blocks": [
                    {"name": "Edit", "input": {}},
                    {"name": "Read", "input": {}},
                ]
            }
        ),
    ]
    assert compute_tools_used(events) == {"Edit": 3, "Bash": 1, "Read": 1}


def test_tools_used_empty() -> None:
    assert compute_tools_used([]) == {}


def test_tools_used_handles_missing_blocks() -> None:
    events = [
        _ev({}),
        _ev({"tool_use_blocks": []}),
        {"raw_data": "garbage"},
    ]
    assert compute_tools_used(events) == {}


def test_tool_projection_excludes_large_unneeded_raw_fields() -> None:
    """S3 pages deterministic tool facts, never an arbitrary tool transcript."""
    huge = "x" * (2 * 1024 * 1024)
    raw = json.dumps(
        {
            "tool_use_blocks": [
                {"name": "Edit", "input": {"path": "src/main.py", "command": huge}}
            ],
            "details": huge,
        }
    )
    with duckdb.connect() as con:
        con.execute("CREATE TABLE events(raw_data VARCHAR)")
        con.execute("INSERT INTO events VALUES (?)", [raw])
        projected = con.execute(
            f"SELECT {_tool_projection_sql()} AS raw_data FROM events"
        ).fetchone()[0]
    assert len(projected.encode()) < 1024
    assert json.loads(projected) == {
        "tool_name": "Edit",
        "tool_use_blocks": [
            {"name": "Edit", "input": {"path": "src/main.py", "file_path": None}}
        ],
        "tool": {"name": None, "input": {"path": None, "file_path": None}},
        "input": {"path": None, "file_path": None},
        "arguments": {"path": None, "file_path": None},
        "path": None,
        "file_path": None,
        "derived_files": [],
    }


def test_tool_projection_derives_patch_paths_without_returning_command() -> None:
    command = """apply_patch <<'PATCH'
*** Begin Patch
*** Update File: tests/test_memory_integrity.py
@@
*** End Patch
PATCH"""
    raw = json.dumps({"tool": "shell", "input": {"command": command}})
    with duckdb.connect() as con:
        con.execute("CREATE TABLE events(raw_data VARCHAR)")
        con.execute("INSERT INTO events VALUES (?)", [raw])
        projected = con.execute(
            f"SELECT {_tool_projection_sql()} AS raw_data FROM events"
        ).fetchone()[0]
    assert "apply_patch" not in projected
    assert compute_files_touched([{"raw_data": projected}]) == [
        "tests/test_memory_integrity.py"
    ]


def test_tool_projection_preserves_flat_tool_input() -> None:
    raw = json.dumps(
        {
            "tool": "Edit",
            "input": {"file_path": "src/drover/server/memory_identity.py"},
        }
    )
    with duckdb.connect() as con:
        con.execute("CREATE TABLE events(raw_data VARCHAR)")
        con.execute("INSERT INTO events VALUES (?)", [raw])
        projected = con.execute(
            f"SELECT {_tool_projection_sql()} AS raw_data FROM events"
        ).fetchone()[0]
    projected_event = {"event_type": "tool_action", "raw_data": projected}
    assert compute_files_touched([projected_event]) == [
        "src/drover/server/memory_identity.py"
    ]
    assert compute_tools_used([projected_event]) == {"Edit": 1}


def test_tool_projection_of_invalid_json_is_null() -> None:
    with duckdb.connect() as con:
        con.execute("CREATE TABLE events(raw_data VARCHAR)")
        con.executemany(
            "INSERT INTO events VALUES (?)", [["garbage"], [""], [None], ["5"]]
        )
        projected = con.execute(
            f"SELECT {_tool_projection_sql()} FROM events ORDER BY rowid"
        ).fetchall()
    assert [row[0] for row in projected[:3]] == [None, None, None]
    # Valid JSON without tool fields projects to the all-null shape, as before.
    assert json.loads(projected[3][0])["derived_files"] == []


def test_tool_facts_of_large_events_fit_the_query_child_memory_limit() -> None:
    """Regression: an active session's big events no longer exhaust DuckDB.

    Prod, 2026-10-06: tool facts for a live Claude Code session failed with
    ``OutOfMemoryException`` at the query child's 768 MB limit (reported as a
    bare ``analytics_unavailable``) although its 25k-event tail held 17 MB of
    raw_data: the projection parsed each event once per extracted field. The
    same failure at a scaled-down limit: 20 events of 200 KB in 64 MB.
    """
    from drover.server.summarizer import worker

    big = "x" * 200_000
    with duckdb.connect(
        config={"memory_limit": "64MB", "threads": 1, "preserve_insertion_order": False}
    ) as con:
        con.execute("SET TimeZone='UTC'")
        con.execute(
            "CREATE TABLE agent_events(id VARCHAR, session_id VARCHAR,"
            " dedup_key VARCHAR, repo_owner VARCHAR, repo_name VARCHAR,"
            " timestamp TIMESTAMPTZ, event_type VARCHAR, raw_data VARCHAR)"
        )
        rows = [
            (
                f"id-{i}",
                "s",
                f"k-{i}",
                "o",
                "r",
                f"2026-10-06 00:00:{i:02d}+00",
                "tool_result",
                json.dumps(
                    {
                        "tool_use_blocks": [
                            {"name": "Edit", "input": {"file_path": f"f{i}.py"}}
                        ],
                        "message": {"content": [{"type": "text", "text": big}]},
                    }
                ),
            )
            for i in range(20)
        ]
        con.executemany("INSERT INTO agent_events VALUES (?,?,?,?,?,?,?,?)", rows)
        files, tools = worker._read_tool_facts(con, "s", None)
    assert files == sorted(f"f{i}.py" for i in range(20))
    assert tools == {"Edit": 20}


def test_failure_text_keeps_the_lake_cause() -> None:
    from drover.server.lake.runtime import LakeError
    from drover.server.summarizer.worker import _classify_failure, _describe

    exc = LakeError(
        "analytics_memory_limit_exceeded",
        "OutOfMemoryException: Out of Memory Error: failed to allocate data",
    )
    assert _describe(exc) == (
        "analytics_memory_limit_exceeded: OutOfMemoryException: "
        "Out of Memory Error: failed to allocate data"
    )
    assert _classify_failure(exc) == (True, "runtime")
    assert _describe(LakeError("analytics_deadline_exceeded")) == (
        "analytics_deadline_exceeded"
    )
    assert _describe(RuntimeError("no events for session s")) == (
        "no events for session s"
    )


def _page_fixture(con) -> None:
    """Ties on timestamp/id, duplicate dedup keys, NULL raw_data, another session."""
    con.execute("SET TimeZone='UTC'")
    con.execute(
        "CREATE TABLE agent_events(id VARCHAR, session_id VARCHAR, dedup_key VARCHAR,"
        " repo_owner VARCHAR, repo_name VARCHAR, timestamp TIMESTAMPTZ,"
        " event_type VARCHAR, raw_data VARCHAR)"
    )
    rows = []
    for i in range(2500):
        raw = json.dumps(
            {
                "tool_name": f"Tool{i % 7}",
                "input": {"path": f"src/f{i % 13}.py", "command": "x" * (i % 50)},
            }
        )
        rows.append(
            (
                f"id-{i // 3}",  # duplicate ids force the hash tie-break
                "s",
                f"k-{i // 2}" if i % 5 else None,  # duplicate keys + NULL keys
                "o" if i % 4 else None,
                "r" if i % 4 else None,
                f"2026-10-01 00:{(i // 100) % 60:02d}:00+00",  # timestamp ties
                "tool_action",
                None if i % 97 == 0 else raw,
            )
        )
    rows.append(("other", "t", "k-other", "o", "r", "2026-10-01", "x", "{}"))
    con.executemany("INSERT INTO agent_events VALUES (?,?,?,?,?,?,?,?)", rows)


def _newest_projected(con, limit: int) -> list[dict]:
    """The unpaged reference: newest ``limit`` canonical raw events, projected."""
    from drover.server.summarizer.worker import _session_agent_events_ctes

    rows = con.execute(
        f"""WITH {_session_agent_events_ctes()}
        SELECT event_type, {_tool_projection_sql()} AS raw_data FROM (
          SELECT event_type, raw_data FROM canonical_agent_events
          WHERE raw_data IS NOT NULL
          ORDER BY timestamp DESC, coalesce(id, '') DESC,
                   coalesce(dedup_key, '') DESC, hash(raw_data) DESC
          LIMIT ?)""",
        ["s", limit],
    ).fetchall()
    return [{"event_type": t, "raw_data": r} for t, r in rows]


def test_tool_fact_groups_match_the_unpaged_projection(monkeypatch) -> None:
    """Grouped, paged tool facts derive exactly what per-event rows did."""
    from drover.server.summarizer import worker

    monkeypatch.setattr(worker, "MAX_RAW_EVENTS_PER_SESSION", 1500)
    monkeypatch.setattr(worker, "RAW_EVENT_PAGE_ROWS", 10)  # force group paging
    with duckdb.connect() as con:
        _page_fixture(con)
        expected = _newest_projected(con, 1500)
        files, tools = worker._read_tool_facts(con, "s", None)
    assert len(expected) == 1500
    assert files == compute_files_touched(expected)
    assert tools == compute_tools_used(expected)
    assert sum(tools.values()) == 1500


def test_tool_facts_bounded_to_the_tail_match_the_unbounded_read(
    monkeypatch,
) -> None:
    from drover.server.summarizer import worker

    monkeypatch.setattr(worker, "MAX_RAW_EVENTS_PER_SESSION", 700)
    with duckdb.connect() as con:
        _page_fixture(con)
        tail_lower, _, stored, _ = worker._session_bounds(con, "s")
        assert tail_lower is not None and stored == 2500
        # The tail is the newest 700 *stored* events (ties included); the
        # dedup window then collapses duplicates among them.
        rows = con.execute(
            f"""WITH {worker._session_agent_events_ctes()}
            SELECT event_type, {_tool_projection_sql()} FROM canonical_agent_events
            WHERE raw_data IS NOT NULL AND timestamp >= ?""",
            ["s", tail_lower],
        ).fetchall()
        expected = [{"event_type": t, "raw_data": r} for t, r in rows]
        assert 0 < len(expected) < 700
        files, tools = worker._read_tool_facts(con, "s", tail_lower)
    # The fixture's exact dedup ties (same key, repo, timestamp and id, other
    # payload) are an arbitrary pick in any read, so compare what is stable.
    assert files == compute_files_touched(expected)
    assert sum(tools.values()) == sum(compute_tools_used(expected).values())
    assert tools.keys() == compute_tools_used(expected).keys()


def test_tool_facts_project_only_the_chosen_events() -> None:
    from drover.server.summarizer.worker import _tool_facts_sql

    for lower in (False, True):
        for after in (False, True):
            sql = _tool_facts_sql(lower=lower, after=after)
            chosen = sql.index("recent_events AS (")
            # The JSON/regex projection runs only after the newest-N LIMIT.
            assert sql.index("LIMIT ?") < sql.index("json_object") > chosen
            assert "json_" not in sql[chosen : sql.index("LIMIT ?")]


def _long_session(con, events: int) -> None:
    """Substantive turns only early on, then a long non-substantive tail."""
    con.execute("SET TimeZone='UTC'")
    con.execute(
        "CREATE TABLE agent_events(id VARCHAR, session_id VARCHAR,"
        " dedup_key VARCHAR, repo_owner VARCHAR, repo_name VARCHAR,"
        " timestamp TIMESTAMPTZ, event_type VARCHAR, role VARCHAR,"
        " content VARCHAR, agent_id VARCHAR, task_id VARCHAR, raw_data VARCHAR)"
    )
    rows = []
    for i in range(events):
        ts = f"2026-08-{15 + i // 86400:02d} {i // 3600 % 24:02d}:{i // 60 % 60:02d}:{i % 60:02d}+00"
        if i < events // 10:
            role = ("user", "assistant", "tool")[i % 3]
            kind = "tool_result" if role == "tool" else "message"
            # A final assistant reply older than the newest 30 turns.
            if i >= events // 10 - 40 and role == "assistant":
                kind = "tool_call"
            raw = json.dumps({"tool_name": f"T{i % 5}", "input": {"path": f"f{i}"}})
            rows.append(
                (
                    f"id-{i}",
                    "s",
                    f"k-{i // 2}",
                    "o",
                    "r",
                    ts,
                    kind,
                    role,
                    f"turn {i}",
                    "agent",
                    "task",
                    raw,
                )
            )
        else:
            rows.append(
                (
                    f"id-{i}",
                    "s",
                    f"k-{i // 2}",
                    None,
                    None,
                    ts,
                    "attachment",
                    None,
                    "",
                    "agent",
                    None,
                    '{"type": "attachment"}',
                )
            )
    rows.append(
        (
            "other",
            "t",
            "k-other",
            "o",
            "r",
            "2026-08-15",
            "message",
            "user",
            "elsewhere",
            "agent",
            "task",
            "{}",
        )
    )
    con.executemany("INSERT INTO agent_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)


def test_sliced_prompt_window_matches_the_unbounded_window(monkeypatch) -> None:
    """Walking time slices newest first finds the same turns as one read."""
    from drover.server.summarizer import worker
    from drover.server.summarizer.derive import select_substantive_window

    monkeypatch.setattr(worker, "MAX_RAW_EVENTS_PER_SESSION", 250)
    monkeypatch.setattr(worker, "SESSION_SLICE_EVENTS", 400)
    reads = []
    with duckdb.connect() as con:
        _long_session(con, 6000)
        whole = select_substantive_window(con, worker._session_agent_events_ctes(), "s")
        tail_lower, slices, stored, capped = worker._session_bounds(con, "s")
        assert stored == 6000 and len(slices) == 15 and not capped
        assert tail_lower is not None
        real = con.execute

        class Counting:
            def execute(self, sql, params=None):
                reads.append(sql)
                return real(sql, params)

        sliced = worker._read_prompt_window(Counting(), "s", slices, capped)
    assert [e["id"] for e in sliced] == [e["id"] for e in whole]
    assert sliced == whole
    assert any(worker._is_final_assistant(e) for e in sliced)
    # Substantive turns sit in the oldest 600 events: the walk reaches them
    # and stops there, short of reading the whole history at once.
    assert 1 < len(reads) <= len(slices) + 1


def test_prompt_walk_stops_at_the_scan_cap(monkeypatch) -> None:
    """Beyond MAX_PROMPT_SCAN_EVENTS a session has no prompt: an input fault."""
    from drover.server.summarizer import worker

    monkeypatch.setattr(worker, "SESSION_SLICE_EVENTS", 400)
    monkeypatch.setattr(worker, "MAX_PROMPT_SCAN_EVENTS", 2000)
    with duckdb.connect() as con:
        _long_session(con, 6000)
        _, slices, _, capped = worker._session_bounds(con, "s")
        assert capped and len(slices) == 5
        assert worker._read_prompt_window(con, "s", slices, capped) == []


def test_short_session_reads_once_without_bounds() -> None:
    from drover.server.summarizer import worker

    with duckdb.connect() as con:
        _long_session(con, 300)
        assert worker._session_bounds(con, "s") == (None, [], 0, False)


def test_any_value_lookups_stop_at_the_newest_slice_with_a_value(
    monkeypatch,
) -> None:
    from drover.server.summarizer import worker

    monkeypatch.setattr(worker, "SESSION_SLICE_EVENTS", 400)
    with duckdb.connect() as con:
        _long_session(con, 6000)
        _, slices, _, capped = worker._session_bounds(con, "s")
        # Only the oldest events carry a task and a repository.
        assert worker._safe_task_id(con, "s", slices, capped) == "task"
        assert worker._first_in_slices(
            con,
            "s",
            "SELECT any_value(repo_owner), any_value(repo_name) FROM "
            "canonical_agent_events WHERE repo_owner IS NOT NULL",
            slices,
            capped,
        ) == ("o", "r")
