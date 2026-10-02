"""Web History view: the pure module under node, and the page as a component.

static/history_view.js holds every rule (query building, day grouping, the
virtual window, debounce, snapshots, transcript paging). The page tests boot
history.html's real script against a stub DOM (tests/fixtures/web/
history_page_harness.js) with canned hub responses.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from drover.server.web.ui import load_page

STATIC = Path(__file__).resolve().parents[1] / "src/drover/server/web/static"
MODULE = STATIC / "history_view.js"
HARNESS = Path(__file__).parent / "fixtures/web/history_page_harness.js"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(
    NODE is None, reason="node is required for web JS tests"
)

NOW = "2026-10-02T12:00:00Z"


def _now_ms() -> int:
    from datetime import datetime

    return int(datetime.fromisoformat(NOW.replace("Z", "+00:00")).timestamp() * 1000)


def _js(expressions: dict[str, str]) -> dict:
    script = f"""
const H = require({json.dumps(str(MODULE))});
const exprs = {json.dumps(expressions)};
const out = {{}};
for (const [key, src] of Object.entries(exprs)) out[key] = eval(src);
process.stdout.write(JSON.stringify(out));
"""
    proc = subprocess.run(
        [NODE, "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
        env={"TZ": "UTC", "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _item(i: int, at: str, **extra) -> dict:
    return {
        "id": f"s-{i}",
        "title": f"Task {i}",
        "harness": "agent",
        "model": "m",
        "host": {"id": "mac", "name": "Studio Mac", "retired": False},
        "repo": "o/r",
        "branch": "main",
        "state": "finished",
        "status": "completed",
        "started_at": at,
        "ended_at": at,
        "last_activity": at,
        "summary": f"Summary {i}",
        "has_transcript": True,
        "tokens": {"input": 1200, "output": 300, "cache_read": 0, "cache_write": 0},
        **extra,
    }


# --------------------------------------------------------------------------- #
# Module                                                                      #
# --------------------------------------------------------------------------- #


@needs_node
def test_query_building_is_stable_and_bounded():
    out = _js(
        {
            "url": "H.pageUrl({states: ['failed', 'running'], hosts: ['b', 'a', 'a'], q: '  pager '})",
            "next": "H.pageUrl({}, 'CUR', 50)",
            "until": "H.pageUrl({until: '2026-09-30'})",
            "key_same": "H.filtersKey({hosts: ['a', 'b']}) === H.filtersKey({hosts: ['b', 'a']})",
            "round": "H.filtersFromSearch('?host=a&state=failed&state=bogus&q=x&repo=o%2Fr')",
            "toggled": "H.toggle(H.toggle({}, 'hosts', 'a'), 'hosts', 'a').hosts",
            "has": "[H.hasFilters({}), H.hasFilters({q: ' '}), H.hasFilters({states: ['failed']})]",
            "range": "H.rangeFilters({}, 7, Date.parse('2026-10-02T00:00:00Z')).since",
        }
    )
    assert (
        out["url"]
        == "/sessions/history?host=a&host=b&state=failed&state=running&q=pager&limit=30"
    )
    assert out["next"] == "/sessions/history?limit=50&cursor=CUR"
    assert "until=2026-10-01" in out["until"]  # inclusive "to" date
    assert out["key_same"] is True
    assert out["round"]["hosts"] == ["a"] and out["round"]["states"] == ["failed"]
    assert out["round"]["repos"] == ["o/r"] and out["round"]["q"] == "x"
    assert out["toggled"] == []
    assert out["has"] == [False, False, True]
    assert out["range"] == "2026-09-25T00:00:00.000Z"


@needs_node
def test_merge_dedupes_and_rows_group_by_day():
    items = [
        _item(1, "2026-10-02T09:00:00Z"),
        _item(2, "2026-10-02T01:00:00Z"),
        _item(3, "2026-10-01T22:00:00Z"),
        _item(4, "2026-09-20T10:00:00Z"),
    ]
    out = _js(
        {
            "merged": f"H.merge({json.dumps(items[:2])}, {json.dumps(items[1:])}).map(i => i.id)",
            "utc": f"H.rows({json.dumps(items)}, Date.parse('{NOW}'), 0).map(r => r.kind === 'day' ? r.label : r.key)",
            # In UTC-3, 01:00Z on the 2nd is still the 1st.
            "west": f"H.rows({json.dumps(items)}, Date.parse('{NOW}'), -180).filter(r => r.kind === 'day').map(r => r.key)",
        }
    )
    assert out["merged"] == ["s-1", "s-2", "s-3", "s-4"]
    assert out["utc"][:5] == ["Today", "s-1", "s-2", "Yesterday", "s-3"]
    assert out["utc"][5].startswith("Sun") and out["utc"][6] == "s-4"
    assert out["west"] == ["2026-10-02", "2026-10-01", "2026-09-20"]


@needs_node
def test_virtual_window_keeps_the_dom_small_for_10k_rows():
    out = _js({"window": """(() => {
              const items = Array.from({length: 10000}, (_, i) => ({
                id: 's' + i,
                last_activity: new Date(Date.parse('2026-10-02T00:00:00Z') - i * 600000).toISOString()}));
              const rows = H.rows(items, Date.parse('2026-10-02T12:00:00Z'), 0);
              const lay = H.layout(rows);
              const top = H.visibleRange(lay, 0, 800);
              const deep = H.visibleRange(lay, lay.total / 2, 800);
              const end = H.visibleRange(lay, lay.total - 800, 800);
              return {rows: rows.length, total: lay.total, top, deep, end,
                      more: H.shouldLoadMore(end, rows.length, true, false),
                      busy: H.shouldLoadMore(end, rows.length, true, true),
                      notYet: H.shouldLoadMore(deep, rows.length, true, false)};
            })()"""})["window"]
    assert out["rows"] > 10000
    for key in ("top", "deep", "end"):
        span = out[key]["end"] - out[key]["start"]
        assert span <= 800 // 34 + 2 * 8 + 2, (key, out[key])
    assert out["top"]["start"] == 0
    assert out["end"]["end"] == out["rows"]
    assert out["more"] is True and out["busy"] is False and out["notYet"] is False


@needs_node
def test_debounce_retry_after_and_labels():
    out = _js(
        {
            "debounce": """(() => {
              const pending = new Map(); let id = 0; const calls = [];
              const timers = {setTimeout: (fn) => { pending.set(++id, fn); return id; },
                              clearTimeout: (h) => pending.delete(h)};
              const d = H.debounce((v) => calls.push(v), 250, timers);
              d('p'); d('pa'); d('pag');
              for (const [handle, fn] of [...pending]) { pending.delete(handle); fn(); }
              d('x'); d.cancel();
              return {calls, pending: pending.size};
            })()""",
            "retry": "[H.retryAfterMs('3'), H.retryAfterMs(null), H.retryAfterMs('junk'), H.retryAfterMs('9999'), H.retryAfterMs(new Date(Date.parse('2026-10-02T12:00:05Z')).toUTCString(), Date.parse('2026-10-02T12:00:00Z'))]",
            "tones": "['running', 'awaiting', 'failed', 'finished', 'x'].map(H.stateTone)",
            "tokens": "[H.tokenLabel(null), H.tokenLabel({input: 900, output: 99}), H.tokenLabel({input: 1500}), H.tokenLabel({input: 2400000})]",
        }
    )
    assert out["debounce"] == {"calls": ["pag"], "pending": 0}
    assert out["retry"] == [3000, 2000, 2000, 60000, 5000]
    assert out["tones"] == ["ok", "warn", "bad", "muted", "muted"]
    assert out["tokens"] == ["", "999 tok", "2k tok", "2.4M tok"]


@needs_node
def test_transcript_pages_are_capped_at_200_and_messages_are_cleaned():
    out = _js(
        {
            "first": "H.transcriptUrl('a/b', null)",
            "older": "H.transcriptUrl('x', 401, 500)",
            "views": """[
              H.messageView({seq: 1, type: 'user_input', text: 'hi'}),
              H.messageView({seq: 2, role: 'assistant', text: 'x'.repeat(5000)}),
              H.messageView({seq: 3, type: 'terminal.output', payload: {text: '\\u001b[31mred\\u001b[0m'}}),
              H.messageView({seq: 4, type: 'status', payload_unavailable: {reason: 'pruned'}}),
            ].map(m => [m.seq, m.role, m.text.length > 50 ? m.text.length : m.text, m.unavailable])""",
        }
    )
    assert out["first"] == "/harness/sessions/a%2Fb/messages?limit=200"
    assert out["older"] == "/harness/sessions/x/messages?before_seq=401&limit=200"
    assert out["views"] == [
        [1, "user", "hi", False],
        [2, "assistant", 4001, False],
        [3, "terminal", "red", False],
        [4, "status", "", True],
    ]


@needs_node
def test_snapshot_restores_only_matching_fresh_state():
    out = _js(
        {
            "same": "H.restore(H.snapshot({filters: {hosts: ['a']}, items: [{id: 1}], nextCursor: 'c', hasMore: true, scrollTop: 900}, 1000), {hosts: ['a']}, 2000)",
            "other": "H.restore(H.snapshot({filters: {hosts: ['a']}, items: [], scrollTop: 0}, 1000), {hosts: ['b']}, 2000)",
            "stale": "H.restore(H.snapshot({filters: {}, items: [], scrollTop: 0}, 0), {}, 31 * 60 * 1000)",
            "bad": "H.restore('{not json', {}, 0)",
            "big": "H.restore(H.snapshot({filters: {}, items: Array.from({length: 2000}, (_, i) => ({id: i})), nextCursor: 'c', hasMore: true, scrollTop: 5}, 0), {}, 1)",
        }
    )
    assert out["same"]["items"] == [{"id": 1}] and out["same"]["scrollTop"] == 900
    assert out["same"]["complete"] is True and out["same"]["nextCursor"] == "c"
    assert out["other"] is None and out["stale"] is None and out["bad"] is None
    assert out["big"]["complete"] is False and out["big"]["items"] == []
    assert out["big"]["scrollTop"] == 5


# --------------------------------------------------------------------------- #
# Page                                                                        #
# --------------------------------------------------------------------------- #


def test_history_page_is_served_with_the_module_inlined():
    page = load_page("history.html")
    assert "@include" not in page
    assert page.count("const DroverHistory = (() => {") == 1
    for marker in (
        'id="q"',
        'type="search"',
        'id="list"',
        'id="sentinel"',
        'id="drawer"',
    ):
        assert marker in page
    assert 'href="/ui/history"' in load_page("harness.html")


@needs_node
def test_history_page_scripts_parse():
    for script in re.findall(
        r"<script>(.*?)</script>", load_page("history.html"), re.S
    ):
        proc = subprocess.run(
            [NODE, "-e", "new Function(require('fs').readFileSync(0, 'utf8'))"],
            input=script,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, proc.stderr


def _run_page(tmp_path: Path, scenario: dict) -> dict:
    page = tmp_path / "history.html"
    page.write_text(load_page("history.html"), encoding="utf-8")
    spec = tmp_path / "scenario.json"
    spec.write_text(json.dumps({"now": NOW, **scenario}), encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(HARNESS), str(page), str(spec)],
        capture_output=True,
        text=True,
        timeout=60,
        env={"TZ": "UTC", "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _page(items, cursor=None):
    return {
        "items": items,
        "next_cursor": cursor,
        "has_more": cursor is not None,
        "page_size": 30,
        "truncated": False,
        "as_of": NOW,
    }


FACETS = {
    "url": "/sessions/history/facets",
    "status": 200,
    "body": {
        "hosts": [
            {"id": "mac", "name": "Studio Mac", "retired": False},
            {"id": "old", "name": "Old Laptop", "retired": True},
        ],
        "harnesses": ["agent"],
        "repos": ["o/r"],
        "states": ["running", "awaiting", "finished", "failed"],
    },
}


@needs_node
def test_page_loads_groups_by_day_and_pages_by_cursor(tmp_path):
    first = [_item(i, f"2026-10-02T{11 - i // 4:02d}:00:00Z") for i in range(30)]
    second = [_item(29, first[-1]["last_activity"])] + [
        _item(30 + i, "2026-10-01T08:00:00Z") for i in range(5)
    ]
    trace = _run_page(
        tmp_path,
        {
            "responses": [
                FACETS,
                {
                    "url": "/sessions/history?limit=30&cursor=C1",
                    "status": 200,
                    "body": _page(second),
                },
                {
                    "url": "/sessions/history?limit=30",
                    "status": 200,
                    "body": _page(first, "C1"),
                },
            ],
            "steps": [{"label": "scrolled", "scroll": 3000, "intersect": True}],
        },
    )
    boot, scrolled = trace["steps"]
    assert trace["fetches"][:2] == [
        "/sessions/history/facets",
        "/sessions/history?limit=30",
    ]
    assert "/sessions/history?limit=30&cursor=C1" in trace["fetches"]
    assert trace["fetches"].count("/sessions/history?limit=30&cursor=C1") == 1
    assert boot["days"] == ["Today"] and 0 < boot["rows"] <= 30
    assert boot["stateChips"] == 4
    assert any("retired" in chip for chip in boot["hostChips"])
    assert "Yesterday" in scrolled["days"]
    assert len(set(scrolled["ids"])) == len(scrolled["ids"])  # s-29 not duplicated
    assert scrolled["notice"].startswith("35 sessions")


@needs_node
def test_search_is_debounced_into_one_request(tmp_path):
    trace = _run_page(
        tmp_path,
        {
            "responses": [
                FACETS,
                {"url": "/sessions/history?q=pager", "status": 200, "body": _page([])},
                {
                    "url": "/sessions/history?limit=30",
                    "status": 200,
                    "body": _page([_item(1, NOW)]),
                },
            ],
            "steps": [
                {"type": "p"},
                {"type": "pa"},
                {"type": "pager", "label": "typed"},
                {"advance": 300, "label": "settled"},
            ],
        },
    )
    queries = [url for url in trace["fetches"] if "q=" in url]
    assert queries == ["/sessions/history?q=pager&limit=30"]
    settled = trace["steps"][-1]
    assert settled["notice"] == "No sessions match these filters."
    assert settled["clearHidden"] is False
    assert ["replace", "/ui/history?q=pager"] in trace["history"]


@needs_node
def test_empty_busy_and_unavailable_states(tmp_path):
    empty = _run_page(
        tmp_path,
        {
            "responses": [
                FACETS,
                {"url": "/sessions/history", "status": 200, "body": _page([])},
            ]
        },
    )
    assert empty["steps"][0]["notice"].startswith("No sessions yet")

    busy = _run_page(
        tmp_path,
        {
            "responses": [
                FACETS,
                {
                    "url": "/sessions/history?",
                    "status": 503,
                    "headers": {"Retry-After": "3"},
                    "body": {"error": "history busy"},
                },
                {
                    "url": "/sessions/history?",
                    "status": 200,
                    "body": _page([_item(1, NOW)]),
                },
            ],
            "steps": [
                {"advance": 2999, "label": "waiting"},
                {"advance": 1, "label": "retried"},
            ],
        },
    )
    boot, waiting, retried = busy["steps"]
    assert "Hub busy" in boot["notice"] and "3s" in boot["notice"]
    assert busy["fetches"].count("/sessions/history?limit=30") == 2
    assert waiting["rows"] == 0 and retried["rows"] == 1

    unavailable = _run_page(
        tmp_path,
        {
            "responses": [
                FACETS,
                {"url": "/sessions/history?", "status": 501, "body": {"error": "x"}},
            ]
        },
    )
    assert "PostgreSQL" in unavailable["steps"][0]["notice"]


@needs_node
def test_row_opens_a_paged_transcript_drawer_and_back_keeps_the_list(tmp_path):
    messages_new = {
        "messages": [
            {"seq": 201 + i, "type": "assistant_output", "text": f"m{i}"}
            for i in range(200)
        ],
        "page_min_seq": 201,
        "page_max_seq": 400,
        "max_seq": 400,
        "has_older": True,
        "has_newer": False,
    }
    messages_old = {
        "messages": [{"seq": 1, "type": "user_input", "text": "start"}],
        "page_min_seq": 1,
        "page_max_seq": 1,
        "max_seq": 400,
        "has_older": False,
        "has_newer": True,
    }
    trace = _run_page(
        tmp_path,
        {
            "responses": [
                FACETS,
                {
                    "url": "/harness/sessions/s-1/messages?before_seq=201",
                    "status": 200,
                    "body": messages_old,
                },
                {
                    "url": "/harness/sessions/s-1/messages?limit=200",
                    "status": 200,
                    "body": messages_new,
                },
                {
                    "url": "/sessions/history?",
                    "status": 200,
                    "body": _page([_item(1, NOW), _item(2, NOW)]),
                },
            ],
            "steps": [
                {"clickRow": "s-1", "label": "opened"},
                {"click": "older", "label": "older"},
                {"click": "drawer-close", "label": "closed"},
            ],
        },
    )
    _, opened, older, closed = trace["steps"]
    assert opened["drawerOpen"] and opened["drawerTitle"] == "Task 1"
    assert opened["messages"] == 200 and opened["olderHidden"] is False
    assert older["messages"] == 201 and older["olderHidden"] is True
    assert "/harness/sessions/s-1/messages?before_seq=201&limit=200" in trace["fetches"]
    assert all("limit=200" in url for url in trace["fetches"] if "/messages" in url)
    assert ["push", "/ui/history?session=s-1"] in trace["history"]
    assert closed["drawerOpen"] is False and ["back"] in trace["history"]
    assert closed["rows"] == opened["rows"] == 2  # list never re-fetched
    assert trace["fetches"].count("/sessions/history?limit=30") == 1


@needs_node
def test_back_navigation_restores_the_snapshot_without_refetching(tmp_path):
    items = [_item(i, NOW) for i in range(3)]
    snapshot = json.dumps(
        {
            "key": "state=failed",
            "items": items,
            "nextCursor": "C9",
            "hasMore": True,
            "complete": True,
            "scrollTop": 0,
            "savedAt": _now_ms() - 60_000,
        }
    )
    trace = _run_page(
        tmp_path,
        {
            "search": "?state=failed",
            "navigation": "back_forward",
            "sessionStorage": {"drover.history.v1": snapshot},
            "responses": [FACETS],
            "steps": [{"pagehide": True, "label": "left"}],
        },
    )
    boot, left = trace["steps"]
    assert boot["rows"] == 3
    # Restored rows render without refetching from the top; the short list
    # then continues from the saved cursor, not from page one.
    history_fetches = [
        u for u in trace["fetches"] if u.startswith("/sessions/history?")
    ]
    assert history_fetches == ["/sessions/history?state=failed&limit=30&cursor=C9"]
    saved = json.loads(left["snapshot"])
    assert saved["key"] == "state=failed" and saved["nextCursor"] == "C9"
