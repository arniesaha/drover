"""Session history meets its latency target on a synthetic 10K-session hub.

The target (docs/design/session-history.md) is p95 < 150 ms per page at 10K
sessions, measured end to end through ``fetch_history_page``: SQL, row
rendering and the byte cap. Seeding goes through the real tables, so the
search-document triggers run for every row exactly as they do in production.
"""

from __future__ import annotations

import json
import os
import statistics
import time
from datetime import datetime, timedelta, timezone

from drover.server.db import control_plane_connection
from drover.server.session_history import (
    fetch_history_facets,
    fetch_history_page,
    parse_history_query,
)

SESSIONS = 10_000
P95_TARGET_MS = 150.0


def _seed(path) -> None:
    with control_plane_connection(path) as con:
        con.execute("""INSERT INTO harness_hosts (host_id, display_name, kind, status,
                 capabilities_json, retired_at)
               SELECT 'host-' || g, 'Host ' || g, 'mac', 'online', '{}',
                      CASE WHEN g = 4 THEN now() END
                 FROM generate_series(0, 4) g""")
        con.execute(
            """INSERT INTO harness_sessions (session_id, host_id, harness, command,
                 status, repo_owner, repo_name, branch, model, started_at,
                 updated_at, last_activity, ended_at, awaiting)
               SELECT 'sess-' || lpad(g::text, 6, '0'),
                      'host-' || (g % 5),
                      (ARRAY['claude', 'codex', 'agy'])[1 + g % 3],
                      'agent',
                      (ARRAY['completed', 'completed', 'terminated', 'errored',
                             'running', 'completed', 'failed', 'completed'])[1 + g % 8],
                      'owner' || (g % 7), 'repo-' || (g % 40), 'branch-' || (g % 11),
                      'model-' || (g % 4),
                      timestamptz '2026-01-01' + g * interval '17 minutes',
                      timestamptz '2026-01-01' + g * interval '17 minutes' + interval '9 minutes',
                      timestamptz '2026-01-01' + g * interval '17 minutes' + interval '9 minutes',
                      timestamptz '2026-01-01' + g * interval '17 minutes' + interval '9 minutes',
                      CASE WHEN g % 40 = 4 THEN 'input' END
                 FROM generate_series(1, ?) g""",
            [SESSIONS],
        )
        con.execute(
            """INSERT INTO harness_events (event_id, session_id, event_type,
                 content_preview, created_at, seq)
               SELECT 'ev-' || g, 'sess-' || lpad(g::text, 6, '0'), 'user_input',
                      'prompt ' || g, now(), 1
                 FROM generate_series(1, ?) g WHERE g % 2 = 0""",
            [SESSIONS],
        )
        con.execute(
            """INSERT INTO harness_session_previews (session_id, event_id,
                 content_preview, event_type, event_priority, seq, event_created_at)
               SELECT 'sess-' || lpad(g::text, 6, '0'), 'ev-' || g,
                      'Please ' || (ARRAY['fix', 'refactor', 'investigate', 'document',
                        'benchmark'])[1 + g % 5] || ' the ' ||
                      (ARRAY['websocket reconnect', 'pager window', 'push delivery',
                        'cursor encoding', 'migration ordering', 'flaky test'])[1 + g % 6]
                      || ' in module ' || (g % 97) || repeat(' detail', 20),
                      'user_input', 0, 1, now()
                 FROM generate_series(1, ?) g""",
            [SESSIONS],
        )
        con.execute(
            """INSERT INTO session_memory (session_id, phase, summary_md,
                 summary_generated_at)
               SELECT 'sess-' || lpad(g::text, 6, '0'), 'final',
                      '## Summary' || chr(10) || '- Changed ' || (g % 13) ||
                      ' files around the ' ||
                      (ARRAY['relay', 'hub', 'harnessd', 'summarizer'])[1 + g % 4] ||
                      ' and verified with tests. ' || repeat('Notes on the change. ', 40)
                      || CASE WHEN g % 500 = 0 THEN ' quokka' ELSE '' END,
                      now()
                 FROM generate_series(1, ?) g WHERE g % 4 <> 0""",
            [SESSIONS],
        )
        con.execute(
            """INSERT INTO session_usage (session_id, input_tokens, output_tokens,
                 cache_read_tokens, cache_write_tokens, source, source_seq,
                 source_event_count)
               SELECT 'sess-' || lpad(g::text, 6, '0'), g * 10, g * 3, g * 50, g,
                      'seed', 1, 1
                 FROM generate_series(1, ?) g""",
            [SESSIONS],
        )
        con.execute("ANALYZE")


def _measure(path, params: dict[str, list[str]]) -> tuple[float, dict]:
    started = time.perf_counter()
    page = fetch_history_page(path, parse_history_query(params))
    return (time.perf_counter() - started) * 1000, page


def test_history_p95_under_150ms_at_10k_sessions(pg_control_path):
    path = pg_control_path
    seed_started = time.perf_counter()
    _seed(path)
    seed_s = time.perf_counter() - seed_started
    with control_plane_connection(path) as con:
        assert con.execute("SELECT count(*) FROM harness_sessions").fetchone() == (
            SESSIONS,
        )
        assert con.execute("SELECT count(*) FROM session_search").fetchone() == (
            SESSIONS,
        )

    since = (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=60)).isoformat()
    until = (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=90)).isoformat()
    scenarios: dict[str, dict[str, list[str]]] = {
        "first_page": {},
        "first_page_max": {"limit": ["50"]},
        "host": {"host": ["host-2"]},
        "retired_host": {"host": ["host-4"]},
        "harness": {"harness": ["codex"]},
        "repo": {"repo": ["owner3/repo-17"]},
        "state_failed": {"state": ["failed"]},
        "state_awaiting": {"state": ["awaiting"]},
        "date_range": {"since": [since], "until": [until]},
        "q_common": {"q": ["websocket"]},
        "q_prefix": {"q": ["refac"]},
        "q_rare": {"q": ["quokka"]},
        "q_and_filters": {"q": ["pager"], "host": ["host-1"], "state": ["finished"]},
    }
    samples: dict[str, list[float]] = {name: [] for name in scenarios}
    # Warm the pool and plan cache once; measure afterwards.
    for params in scenarios.values():
        _measure(path, params)
    for _ in range(8):
        for name, params in scenarios.items():
            elapsed, _ = _measure(path, params)
            samples[name].append(elapsed)

    # Deep keyset paging: walk 40 consecutive pages (1,200 rows) from the top
    # and 10 more starting deep in history. Cost must not grow with depth.
    deep: list[float] = []
    cursor = None
    for _ in range(40):
        elapsed, page = _measure(path, {"cursor": [cursor]} if cursor else {})
        deep.append(elapsed)
        cursor = page["next_cursor"]
    old = {"until": ["2026-02-01"]}
    cursor = None
    for _ in range(10):
        params = {**old, **({"cursor": [cursor]} if cursor else {})}
        elapsed, page = _measure(path, params)
        deep.append(elapsed)
        cursor = page["next_cursor"]
    samples["deep_pages"] = deep

    facets = []
    for _ in range(10):
        started = time.perf_counter()
        fetch_history_facets(path)
        facets.append((time.perf_counter() - started) * 1000)
    samples["facets"] = facets

    every = sorted(value for values in samples.values() for value in values)
    p95 = every[int(len(every) * 0.95) - 1]
    report = {
        "sessions": SESSIONS,
        "seed_seconds": round(seed_s, 2),
        "requests": len(every),
        "p50_ms": round(statistics.median(every), 2),
        "p95_ms": round(p95, 2),
        "max_ms": round(every[-1], 2),
        "per_scenario_p95_ms": {
            name: round(sorted(values)[max(0, int(len(values) * 0.95) - 1)], 2)
            for name, values in samples.items()
        },
    }
    print("\nsession-history perf " + json.dumps(report, indent=2))
    out = os.environ.get("DROVER_HISTORY_PERF_REPORT")
    if out:
        with open(out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
    assert p95 < P95_TARGET_MS, report
    for name, values in samples.items():
        assert sorted(values)[max(0, int(len(values) * 0.95) - 1)] < P95_TARGET_MS, (
            name,
            report,
        )
