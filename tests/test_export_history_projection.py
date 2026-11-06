"""Real SQL regressions for narrow-first historical winner selection."""

import duckdb
import pytest
from drover.server.lake.export_worker import _merge_events, _stage
from drover.server.lake.rebuild_worker import POLICY_SCHEMA, LINEAGE

SCHEMA = POLICY_SCHEMA | LINEAGE


def event(key, ident, day="2026-10-31", **kw):
    return dict(
        dedup_key=key,
        id=ident,
        date=day,
        timestamp=day + "T12:00:00Z",
        _import_file=ident,
        _import_row=0,
        _row_sha256=ident + kw.get("content", ""),
        **kw,
    )


class Recording:
    def __init__(self, con):
        self.con, self.queries = con, []

    def execute(self, sql, *args):
        self.queries.append(sql)
        return self.con.execute(sql, *args)


@pytest.mark.parametrize(
    "history,incoming",
    [
        ([], [event("new", "n")]),
        ([event("unrelated", "u")], [event("new", "n")]),
        ([event("same", "old", "2026-10-30")], [event("same", "new")]),
        ([event("same", "old", "2026-11-01")], [event("same", "new")]),
        ([event("same", "a"), event("same", "z")], [event("same", "m")]),
        (
            [event("same", "a", repo_owner="owner", repo_name="repo")],
            [event("same", "z")],
        ),
        (
            [event("same", "tie", content='{"text":"a"}')],
            [event("same", "tie", content='{"text":"b"}')],
        ),
    ],
)
def test_narrow_first_matches_original_winners(history, incoming):
    # Run the exact original affected selection as the independent baseline;
    # all subsequent ranking/delete/insert statements are shared unchanged.
    results = []
    for legacy in (True, False):
        with duckdb.connect() as con:
            con.execute("CREATE SCHEMA lake")
            _stage(
                con, "history", history or [event("placeholder", "placeholder")], SCHEMA
            )
            if not history:
                con.execute("DELETE FROM history")
            con.execute("CREATE TABLE lake.agent_events AS SELECT * FROM history")
            _stage(con, "export_events", incoming, SCHEMA)
            recorder = Recording(con)
            if legacy:

                class Legacy(Recording):
                    def execute(self, sql, *args):
                        if "CREATE TEMP TABLE affected_keys" in sql:
                            sql = "CREATE TEMP TABLE affected_keys AS SELECT dedup_key FROM export_events"
                        if "FROM affected_keys)" in sql:
                            sql = sql.replace(
                                "FROM affected_keys)", "FROM export_events)"
                            )
                        return super().execute(sql, *args)

                recorder = Legacy(con)
            con.execute("BEGIN")
            _merge_events(recorder)
            results.append(
                con.execute(
                    "SELECT * FROM lake.agent_events ORDER BY dedup_key,id,_row_sha256"
                ).fetchall()
            )
            if not legacy:
                overlap = bool(
                    {r["dedup_key"] for r in history}
                    & {r["dedup_key"] for r in incoming}
                )
                wide = [
                    q
                    for q in recorder.queries
                    if "CREATE TEMP TABLE affected AS SELECT t.*" in q
                ]
                assert bool(wide) == overlap
                assert all(
                    "date" not in q
                    for q in recorder.queries
                    if "CREATE TEMP TABLE affected_keys" in q
                )
                assert con.execute("SELECT count(*) FROM affected_keys").fetchone()[
                    0
                ] == int(overlap)
            con.execute("ROLLBACK")
    assert results[0] == results[1]
