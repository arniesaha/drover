"""Session history: keyset paging, filters, full-text search, caps, migration."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import pytest

from drover.server import session_history as history
from drover.server.db import control_plane_connection
from drover.server.harness.registry import HarnessRegistry
from drover.server.session_history import (
    MAX_PAGE_SIZE,
    RESPONSE_BYTES,
    SUMMARY_CHARS,
    TITLE_CHARS,
    HistoryQuery,
    fetch_history_facets,
    fetch_history_page,
    parse_history_query,
    text_search_query,
)

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def _set_activity(path, session_id: str, at: datetime) -> None:
    with control_plane_connection(path) as con:
        con.execute(
            "UPDATE harness_sessions SET last_activity = ? WHERE session_id = ?",
            [at, session_id],
        )


def _summary(path, session_id: str, text: str, *, final: bool = True) -> None:
    with control_plane_connection(path) as con:
        if final:
            con.execute(
                """INSERT INTO session_memory
                   (session_id, phase, summary_md, summary_generated_at)
                   VALUES (?, 'final', ?, now())""",
                [session_id, text],
            )
        else:
            con.execute(
                """INSERT INTO session_memory
                   (session_id, phase, recap_text, recap_source_seq)
                   VALUES (?, 'live', ?, 1)""",
                [session_id, text],
            )


def _session(
    registry: HarnessRegistry,
    path,
    sid: str,
    at: datetime,
    *,
    host: str = "mac",
    harness: str = "claude",
    status: str = "completed",
    repo: tuple[str, str] | None = ("arniesaha", "drover"),
    branch: str | None = "main",
    prompt: str | None = None,
) -> str:
    registry.create_session(
        session_id=sid,
        host_id=host,
        harness=harness,
        command=harness,
        status=status,
        repo_owner=repo[0] if repo else None,
        repo_name=repo[1] if repo else None,
        branch=branch,
        started_at=at - timedelta(minutes=5),
        model="claude-opus-5-5",
    )
    if prompt is not None:
        registry.append_event(
            session_id=sid, event_type="user_input", content_preview=prompt, seq=1
        )
    _set_activity(path, sid, at)
    return sid


def _page(path, **params):
    flat = {k: [v] if isinstance(v, str) else list(v) for k, v in params.items()}
    return fetch_history_page(path, parse_history_query(flat))


def _all_ids(path, **params) -> list[str]:
    ids: list[str] = []
    cursor = None
    for _ in range(100):
        page = _page(path, **params, **({"cursor": cursor} if cursor else {}))
        ids.extend(item["id"] for item in page["items"])
        cursor = page["next_cursor"]
        if not page["has_more"]:
            assert cursor is None
            break
    return ids


@pytest.fixture
def fleet(pg_control_path):
    registry = HarnessRegistry(pg_control_path)
    registry.register_host(host_id="mac", display_name="Studio Mac", kind="mac")
    registry.register_host(host_id="linux", display_name="Build Box", kind="linux")
    registry.register_host(host_id="old", display_name="Old Laptop", kind="mac")
    return registry, pg_control_path


# --------------------------------------------------------------------------- #
# Parsing and cursor                                                          #
# --------------------------------------------------------------------------- #


def test_parse_defaults_clamps_and_rejects():
    assert parse_history_query({}).limit == 30
    assert parse_history_query({"limit": ["500"]}).limit == MAX_PAGE_SIZE
    query = parse_history_query(
        {"host": ["mac,linux", "mac"], "state": ["running"], "repo": ["a/b"]}
    )
    assert query.hosts == ("mac", "linux")
    assert query.repos == (("a", "b"),)
    for bad in (
        {"limit": ["0"]},
        {"limit": ["x"]},
        {"state": ["sleeping"]},
        {"repo": ["noslash"]},
        {"since": ["yesterday"]},
        {"since": ["2026-09-02"], "until": ["2026-09-01"]},
        {"offset": ["30"]},
        {"q": ["x" * 201]},
        {"host": [",".join(str(i) for i in range(21))]},
        {"cursor": ["not-a-cursor"]},
    ):
        with pytest.raises(ValueError):
            parse_history_query(bad)
    since = parse_history_query({"since": ["2026-09-01"]}).since
    assert since == datetime(2026, 9, 1, tzinfo=timezone.utc)


def test_cursor_round_trip_and_is_bound_to_its_filters():
    query = HistoryQuery(hosts=("mac",))
    token = history.encode_cursor(T0, "s-1", query.fingerprint())
    assert history.decode_cursor(token, query.fingerprint()) == (T0, "s-1")
    with pytest.raises(ValueError, match="different filters"):
        parse_history_query({"host": ["linux"], "cursor": [token]})


def test_text_search_query_is_built_only_from_word_terms():
    assert (
        text_search_query("Fix   the Flaky-test") == "fix:* & the:* & flaky:* & test:*"
    )
    assert text_search_query("&|!:*()'") is None
    assert text_search_query("") is None
    assert text_search_query(" ".join(f"w{i}" for i in range(20))).count("&") == 7


# --------------------------------------------------------------------------- #
# Keyset paging                                                               #
# --------------------------------------------------------------------------- #


def test_keyset_pages_cover_every_session_once_in_order(fleet):
    registry, path = fleet
    expected = []
    for i in range(75):
        # Every third shares a timestamp, so ties exercise the id tiebreak.
        at = T0 + timedelta(minutes=(i // 3) * 3)
        expected.append(_session(registry, path, f"s-{i:03d}", at))
    pages = []
    cursor = None
    while True:
        page = _page(path, limit="30", **({"cursor": cursor} if cursor else {}))
        pages.append(page)
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    assert [len(p["items"]) for p in pages] == [30, 30, 15]
    ids = [item["id"] for p in pages for item in p["items"]]
    assert len(ids) == len(set(ids)) == 75
    keys = [(item["last_activity"], item["id"]) for p in pages for item in p["items"]]
    assert keys == sorted(keys, reverse=True)
    assert pages[-1]["next_cursor"] is None


def test_keyset_has_no_duplicates_or_gaps_while_new_sessions_arrive(fleet):
    registry, path = fleet
    original = {
        _session(registry, path, f"old-{i:03d}", T0 + timedelta(minutes=i))
        for i in range(60)
    }
    seen: list[str] = []
    cursor = None
    arrivals = 0
    while True:
        page = _page(path, limit="25", **({"cursor": cursor} if cursor else {}))
        seen.extend(item["id"] for item in page["items"])
        # New sessions land between page fetches, newer than everything.
        for _ in range(7):
            _session(
                registry,
                path,
                f"new-{arrivals:03d}",
                T0 + timedelta(days=1, minutes=arrivals),
            )
            arrivals += 1
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    assert len(seen) == len(set(seen))
    assert set(seen) == original
    # A refresh from the top sees the arrivals first.
    top = _page(path, limit="5")["items"]
    assert all(item["id"].startswith("new-") for item in top)


# --------------------------------------------------------------------------- #
# Filters and item shape                                                      #
# --------------------------------------------------------------------------- #


def test_filters_select_by_host_harness_repo_state_and_time(fleet):
    registry, path = fleet
    _session(registry, path, "a", T0, host="mac", harness="claude", status="running")
    _session(
        registry,
        path,
        "b",
        T0 + timedelta(hours=1),
        host="linux",
        harness="codex",
        status="completed",
    )
    _session(
        registry,
        path,
        "c",
        T0 + timedelta(hours=2),
        host="linux",
        harness="codex",
        status="errored",
        repo=("acme", "web"),
    )
    _session(
        registry,
        path,
        "d",
        T0 + timedelta(hours=3),
        host="mac",
        harness="claude",
        status="running",
    )
    with control_plane_connection(path) as con:
        con.execute(
            "UPDATE harness_sessions SET awaiting = 'input' WHERE session_id = 'd'"
        )
    _session(
        registry,
        path,
        "e",
        T0 + timedelta(hours=4),
        host="mac",
        status="terminated",
        repo=None,
    )

    assert _all_ids(path, host="linux") == ["c", "b"]
    assert _all_ids(path, host=["mac", "linux"]) == ["e", "d", "c", "b", "a"]
    assert _all_ids(path, harness="codex") == ["c", "b"]
    assert _all_ids(path, repo="acme/web") == ["c"]
    assert _all_ids(path, state="running") == ["a"]
    assert _all_ids(path, state="awaiting") == ["d"]
    assert _all_ids(path, state="finished") == ["e", "b"]
    assert _all_ids(path, state="failed") == ["c"]
    assert _all_ids(path, state=["failed", "awaiting"]) == ["d", "c"]
    assert _all_ids(
        path,
        since=(T0 + timedelta(hours=1)).isoformat(),
        until=(T0 + timedelta(hours=3)).isoformat(),
    ) == ["c", "b"]
    assert _all_ids(path, host="mac", state="running", harness="claude") == ["a"]


def test_item_shape_is_small_and_labels_retired_hosts(fleet):
    registry, path = fleet
    sid = _session(
        registry,
        path,
        "shape",
        T0,
        host="old",
        prompt="Refactor the pager\nsecond line",
    )
    _summary(
        path,
        sid,
        "## Done\n- **Rewrote** the [pager](http://x) window " + "word " * 200,
    )
    with control_plane_connection(path) as con:
        con.execute(
            """INSERT INTO session_usage (session_id, input_tokens, output_tokens,
               cache_read_tokens, cache_write_tokens, source, source_seq, source_event_count)
               VALUES (?, 10, 20, 30, 40, 'test', 1, 1)""",
            [sid],
        )
    registry.retire_host("old", reason="sold", force=True)
    _session(
        registry,
        path,
        "orphan",
        T0 - timedelta(hours=1),
        host="gone-host",
        repo=None,
        branch=None,
    )

    items = _page(path)["items"]
    item = items[0]
    assert set(item) == {
        "id",
        "title",
        "harness",
        "model",
        "host",
        "repo",
        "branch",
        "state",
        "status",
        "started_at",
        "ended_at",
        "last_activity",
        "summary",
        "has_transcript",
        "tokens",
    }
    assert item["title"] == "Refactor the pager"
    assert item["host"] == {"id": "old", "name": "Old Laptop", "retired": True}
    assert item["repo"] == "arniesaha/drover" and item["branch"] == "main"
    assert item["state"] == "finished" and item["has_transcript"] is True
    assert item["summary"].startswith("Done Rewrote the pager window")
    assert len(item["summary"]) <= SUMMARY_CHARS
    assert item["tokens"] == {
        "input": 10,
        "output": 20,
        "cache_read": 30,
        "cache_write": 40,
    }
    assert item["last_activity"] == T0.isoformat()
    orphan = items[1]
    assert orphan["host"] == {"id": "gone-host", "name": "gone-host", "retired": True}
    assert orphan["title"] == "claude session"
    assert orphan["has_transcript"] is False and orphan["tokens"] is None


def test_title_falls_back_and_is_bounded(fleet):
    registry, path = fleet
    _session(registry, path, "long", T0, prompt="é" * 500)
    _session(registry, path, "repo-only", T0 - timedelta(minutes=1))
    items = {item["id"]: item for item in _page(path)["items"]}
    assert len(items["long"]["title"]) <= TITLE_CHARS
    assert items["repo-only"]["title"] == "arniesaha/drover · main"


# --------------------------------------------------------------------------- #
# Full-text search                                                            #
# --------------------------------------------------------------------------- #


def test_full_text_search_matches_title_summary_and_repo(fleet):
    registry, path = fleet
    _session(registry, path, "t", T0, prompt="Investigate flaky websocket reconnects")
    _session(registry, path, "s", T0 - timedelta(minutes=1), prompt="Hello")
    _summary(path, "s", "Moved the APNs rejection handling into the push layer.")
    _session(
        registry,
        path,
        "r",
        T0 - timedelta(minutes=2),
        repo=("acme", "billing-service"),
        prompt="hi",
    )
    _session(registry, path, "n", T0 - timedelta(minutes=3), prompt="unrelated")

    assert _all_ids(path, q="websocket") == ["t"]
    assert _all_ids(path, q="websock") == ["t"]  # prefix while typing
    assert _all_ids(path, q="apns rejection") == ["s"]
    assert _all_ids(path, q="apns websocket") == []  # all terms must match
    assert _all_ids(path, q="billing") == ["r"]
    assert _all_ids(path, q="acme/billing-service") == ["r"]
    assert _all_ids(path, q="&|!") == ["t", "s", "r", "n"]  # no terms: no filter
    assert _all_ids(path, q="flaky", state="finished") == ["t"]


def test_search_document_follows_previews_memory_by_native_id_and_deletes(fleet):
    registry, path = fleet
    registry.create_session(
        session_id="h1",
        host_id="mac",
        harness="codex",
        command="codex",
        status="completed",
        native_session_id="native-1",
    )
    _set_activity(path, "h1", T0)
    assert _all_ids(path, q="kangaroo") == []
    _summary(path, "native-1", "The kangaroo migration")
    assert _all_ids(path, q="kangaroo") == ["h1"]
    registry.append_event(
        session_id="h1",
        event_type="user_input",
        content_preview="platypus please",
        seq=2,
    )
    assert _all_ids(path, q="platypus") == ["h1"]
    with control_plane_connection(path) as con:
        con.execute("DELETE FROM harness_sessions WHERE session_id = 'h1'")
        assert con.execute(
            "SELECT count(*) FROM session_search WHERE session_id = 'h1'"
        ).fetchone() == (0,)


# --------------------------------------------------------------------------- #
# Caps                                                                        #
# --------------------------------------------------------------------------- #


def test_page_is_capped_at_50_rows_and_64_kib(fleet):
    registry, path = fleet
    for i in range(60):
        sid = _session(
            registry, path, f"big-{i:02d}", T0 + timedelta(minutes=i), prompt="🦘" * 400
        )
        _summary(path, sid, "ü" * 5000)
    page = _page(path, limit="100")
    assert len(page["items"]) <= MAX_PAGE_SIZE
    body = json.dumps(page, separators=(",", ":")) + "\n"
    assert len(body.encode()) <= RESPONSE_BYTES
    # Everything is still reachable through the cursor, once each.
    ids = _all_ids(path, limit="50")
    assert len(ids) == len(set(ids)) == 60


def test_byte_cap_sheds_trailing_rows_and_resumes_without_gaps(fleet, monkeypatch):
    registry, path = fleet
    for i in range(12):
        _session(
            registry,
            path,
            f"c-{i:02d}",
            T0 + timedelta(minutes=i),
            prompt=f"prompt {i}",
        )
    monkeypatch.setattr(
        history, "CAPS", history.ResponseCaps(rows=50, response_bytes=2500)
    )
    first = _page(path, limit="12")
    assert first["truncated"] is True and first["has_more"] is True
    assert 0 < len(first["items"]) < 12
    assert len(json.dumps(first, separators=(",", ":"))) <= 2500
    ids = _all_ids(path, limit="12")
    assert ids == [f"c-{i:02d}" for i in reversed(range(12))]


# --------------------------------------------------------------------------- #
# Migration                                                                   #
# --------------------------------------------------------------------------- #


def _history_version() -> int:
    from drover.server.postgres_schema import _MIGRATIONS

    return next(
        version
        for version, statements in _MIGRATIONS
        if any("session_search" in statement for statement in statements)
    )


def test_history_migration_creates_indexes_and_backfills_on_upgrade(fleet):
    from drover.server.control_store import postgres_control_store

    registry, path = fleet
    _session(registry, path, "pre", T0, prompt="wombat survey")
    version = _history_version()
    # Recreate the pre-migration schema with data already present.
    with control_plane_connection(path) as con:
        con.execute("DROP TABLE session_search")
        con.execute("DROP INDEX harness_sessions_history")
        con.execute("DROP INDEX harness_sessions_history_host")
        con.execute(
            "DELETE FROM control_schema_migrations WHERE version = ?", [version]
        )
    postgres_control_store(path).bootstrap()
    postgres_control_store(path).bootstrap()  # repeatable
    with control_plane_connection(path) as con:
        indexes = {
            row[0]
            for row in con.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()"
            ).fetchall()
        }
        applied = {
            row[0]
            for row in con.execute(
                "SELECT version FROM control_schema_migrations"
            ).fetchall()
        }
    assert {
        "harness_sessions_history",
        "harness_sessions_history_host",
        "harness_sessions_native",
        "session_search_document",
    } <= indexes
    assert version in applied
    assert _all_ids(path, q="wombat") == ["pre"]


def test_search_maintenance_never_blocks_a_session_write(fleet):
    registry, path = fleet
    with control_plane_connection(path) as con:
        con.execute("ALTER TABLE session_memory RENAME TO session_memory_away")
    try:
        _session(registry, path, "still-written", T0, prompt="kept")
        assert registry.get_session("still-written") is not None
    finally:
        with control_plane_connection(path) as con:
            con.execute("ALTER TABLE session_memory_away RENAME TO session_memory")


def test_history_query_plan_uses_the_keyset_index(fleet):
    registry, path = fleet
    for i in range(50):
        _session(registry, path, f"p-{i:02d}", T0 + timedelta(minutes=i))
    query = parse_history_query({})
    cursor_query = HistoryQuery(cursor=(T0 + timedelta(minutes=25), "p-25"))
    with control_plane_connection(path) as con:
        con.execute("ANALYZE harness_sessions")
        con.execute("SET enable_seqscan = off")
        for q in (query, cursor_query):
            sql, params = history.page_sql(q)
            plan = "\n".join(
                row[0] for row in con.execute("EXPLAIN " + sql, params).fetchall()
            )
            assert "harness_sessions_history" in plan
        con.execute("RESET enable_seqscan")


# --------------------------------------------------------------------------- #
# HTTP                                                                        #
# --------------------------------------------------------------------------- #


def _serve(path, tmp_path):
    from drover.server.metrics import MetricsCollector
    from drover.server.web.app import start_metrics_server
    from drover.server.web.auth import AuthSettings

    collector = MetricsCollector(
        duckdb_path=path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        ttl_seconds=60,
    )
    return start_metrics_server(
        host="127.0.0.1",
        port=0,
        collector=collector,
        auth=AuthSettings(enabled=True, api_token="operator"),
    )


def _get(server, path, token="operator"):
    req = urllib.request.Request(
        f"http://127.0.0.1:{server.server_address[1]}{path}",
        headers={"Authorization": f"Bearer {token}"} if token else {},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, dict(resp.headers), json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), json.loads(exc.read())


def test_http_history_contract(fleet, tmp_path):
    registry, path = fleet
    for i in range(35):
        _session(
            registry, path, f"h-{i:02d}", T0 + timedelta(minutes=i), prompt=f"task {i}"
        )
    server = _serve(path, tmp_path)
    try:
        status, headers, body = _get(server, "/sessions/history")
        assert status == 200
        assert headers["Cache-Control"].startswith("no-store")
        assert len(body["items"]) == 30 and body["has_more"] is True
        assert set(body) == {
            "items",
            "next_cursor",
            "has_more",
            "page_size",
            "truncated",
            "as_of",
        }
        status, _, second = _get(
            server, "/sessions/history?" + urlencode({"cursor": body["next_cursor"]})
        )
        assert (
            status == 200
            and len(second["items"]) == 5
            and second["next_cursor"] is None
        )
        status, _, error = _get(server, "/sessions/history?state=nope")
        assert status == 400 and "state" in error["error"]
        status, _, facets = _get(server, "/sessions/history/facets")
        assert status == 200
        assert facets["harnesses"] == ["claude"]
        assert facets["repos"] == ["arniesaha/drover"]
        assert {host["id"] for host in facets["hosts"]} == {"mac", "linux", "old"}
        assert facets["states"] == ["running", "awaiting", "finished", "failed"]
        assert _get(server, "/sessions/history", token=None)[0] == 401
        # Saturated admission answers 503 + Retry-After instead of queueing.
        slots = server.RequestHandlerClass.history_slots
        held = 0
        while slots.acquire(blocking=False):
            held += 1
        try:
            status, headers, error = _get(server, "/sessions/history")
            assert status == 503 and headers["Retry-After"] == "1"
            assert error == {"error": "history busy"}
        finally:
            for _ in range(held):
                slots.release()
        # The existing live list is untouched.
        assert _get(server, "/harness/sessions")[0] == 200
        # A history row opens through the existing paged transcript endpoint.
        status, _, transcript = _get(
            server, "/harness/sessions/h-00/messages?before_seq=1000&limit=200"
        )
        assert status == 200 and transcript["messages"]
    finally:
        server.shutdown()


def test_http_history_without_postgres_is_unavailable(tmp_path):
    from drover.schema import bootstrap

    path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=path)
    server = _serve(path, tmp_path)
    try:
        status, headers, body = _get(server, "/sessions/history")
        assert status == 501 and "PostgreSQL" in body["error"]
        assert "Retry-After" not in headers
    finally:
        server.shutdown()


def test_facets_are_bounded(fleet):
    registry, path = fleet
    for i in range(history.MAX_FACET_REPOS + 5):
        _session(
            registry, path, f"f-{i}", T0 + timedelta(minutes=i), repo=("o", f"r{i}")
        )
    facets = fetch_history_facets(path)
    assert len(facets["repos"]) == history.MAX_FACET_REPOS
    assert facets["repos"][0] == f"o/r{history.MAX_FACET_REPOS + 4}"
