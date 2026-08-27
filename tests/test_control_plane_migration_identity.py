"""The pre-split copy must not resurrect events the control plane deleted.

`migrate_control_plane_tables` decides "already there?" by primary key. For
`harness_events` the primary key is `event_id`, but the row's *identity* is
`dedup_key`. Collapsing a duplicate group deletes event_ids that the pre-split
table still holds, so the next start copies them back -- and because the
pre-split table has no `dedup_key` column, `INSERT ... BY NAME` leaves the
column NULL, where the unique index cannot catch them (DuckDB treats NULLs as
distinct). That is drover#280: dedupe that never holds.
"""

from __future__ import annotations

import datetime as dt
import json

import duckdb

from drover.schema import migrate_control_plane_tables, prune_legacy_harness_events
from drover.server.db import control_plane_path
from drover.server.harness.identity import harness_event_identity
from drover.server.harness.schema import bootstrap_harness_tables

# The pre-split shape: no `seq`, no `dedup_key`. Both arrived in later
# migrations that only ever ran against the control-plane store.
_LEGACY_DDL = """
CREATE TABLE harness_events (
  event_id          VARCHAR PRIMARY KEY,
  session_id        VARCHAR NOT NULL,
  event_type        VARCHAR NOT NULL,
  normalized_type   VARCHAR,
  normalized_source VARCHAR,
  content_preview   VARCHAR,
  payload_json      VARCHAR NOT NULL,
  created_at        TIMESTAMP NOT NULL
);
"""

_CREATED_AT = dt.datetime(2026, 7, 6, 21, 3, 50, 932777)
_PAYLOAD = {"text": "hello"}


def _identity(session_id: str, seq: int | None) -> str:
    return harness_event_identity(
        session_id=session_id,
        seq=seq,
        event_type="assistant_message",
        created_at=_CREATED_AT,
        payload=_PAYLOAD,
    )


def _seed_legacy(
    con: duckdb.DuckDBPyConnection,
    event_ids: list[str],
    *,
    created_at: dict[str, dt.datetime] | None = None,
) -> None:
    """Seed the pre-split table. Rows share an identity unless `created_at` differs."""
    con.execute(_LEGACY_DDL)
    for event_id in event_ids:
        con.execute(
            "INSERT INTO harness_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                event_id,
                "harness-s1",
                "assistant_message",
                "message",
                "structured",
                "hello",
                json.dumps(_PAYLOAD),
                (created_at or {}).get(event_id, _CREATED_AT),
            ],
        )


def _seed_control_plane(registry, event_id: str, *, seq: int | None) -> None:
    con = duckdb.connect(str(registry))
    try:
        bootstrap_harness_tables(con)
        con.execute(
            """
            INSERT INTO harness_events (
              event_id, session_id, event_type, normalized_type,
              normalized_source, content_preview, payload_json,
              created_at, seq, dedup_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                event_id,
                "harness-s1",
                "assistant_message",
                "message",
                "structured",
                "hello",
                json.dumps(_PAYLOAD),
                _CREATED_AT,
                seq,
                _identity("harness-s1", seq),
            ],
        )
    finally:
        con.close()


def test_a_deleted_duplicate_is_not_resurrected_from_the_pre_split_tables(tmp_path):
    """The exact drover#280 shape: dedupe kept one of two identical rows."""
    analytical = tmp_path / "drover.duckdb"
    registry = control_plane_path(analytical)
    # Dedupe collapsed {kept, dropped} to `kept`; both survive pre-split.
    _seed_control_plane(registry, "kept", seq=None)

    con = duckdb.connect(str(analytical))
    try:
        _seed_legacy(con, ["kept", "dropped"])
        migrate_control_plane_tables(con, analytical)
    finally:
        con.close()

    reg = duckdb.connect(str(registry), read_only=True)
    try:
        rows = reg.execute(
            "SELECT event_id, dedup_key FROM harness_events ORDER BY event_id"
        ).fetchall()
    finally:
        reg.close()

    assert [row[0] for row in rows] == [
        "kept"
    ], "the migration resurrected a deliberately-deleted duplicate"
    assert rows[0][1] is not None


def test_a_row_missing_from_the_control_plane_is_still_copied_and_stamped(tmp_path):
    """The guard must not disable the migration it is guarding."""
    analytical = tmp_path / "drover.duckdb"
    registry = control_plane_path(analytical)
    _seed_control_plane(registry, "kept", seq=1)

    con = duckdb.connect(str(analytical))
    try:
        # `genuinely-missing` carries seq=None, so its identity differs from
        # the seeded row's and it is a real gap rather than a duplicate.
        _seed_legacy(con, ["kept", "genuinely-missing"])
        con.execute("UPDATE harness_events SET session_id = 'harness-s1'")
        migrate_control_plane_tables(con, analytical)
    finally:
        con.close()

    reg = duckdb.connect(str(registry), read_only=True)
    try:
        rows = dict(
            reg.execute("SELECT event_id, dedup_key FROM harness_events").fetchall()
        )
    finally:
        reg.close()

    assert set(rows) == {"kept", "genuinely-missing"}
    assert rows["genuinely-missing"] == _identity(
        "harness-s1", None
    ), "a copied row must carry the identity the unique index depends on"


def _legacy_table_exists(analytical) -> bool:
    con = duckdb.connect(str(analytical), read_only=True)
    try:
        return bool(
            con.execute(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_name = 'harness_events' AND table_type = 'BASE TABLE'"
            ).fetchone()[0]
        )
    finally:
        con.close()


def test_the_pre_split_copy_is_kept_while_the_control_plane_is_missing_a_row(tmp_path):
    """The drop is only safe once the control plane demonstrably holds it all."""
    analytical = tmp_path / "drover.duckdb"
    registry = control_plane_path(analytical)
    # Matches the pre-split `kept` row: no seq on either side.
    _seed_control_plane(registry, "kept", seq=None)

    con = duckdb.connect(str(analytical))
    try:
        # A distinct created_at makes this a genuinely absent event rather than
        # a collapsed duplicate of `kept`.
        _seed_legacy(
            con,
            ["kept", "genuinely-missing"],
            created_at={"genuinely-missing": _CREATED_AT + dt.timedelta(seconds=5)},
        )
        report = prune_legacy_harness_events(con, analytical, apply=True)
    finally:
        con.close()

    assert report["missing"] == 1
    assert report["dropped"] is False
    assert _legacy_table_exists(analytical), "dropped a copy still holding rows"


def test_the_pre_split_copy_is_dropped_once_every_row_is_in_the_control_plane(tmp_path):
    analytical = tmp_path / "drover.duckdb"
    registry = control_plane_path(analytical)
    _seed_control_plane(registry, "kept", seq=None)

    con = duckdb.connect(str(analytical))
    try:
        # Both pre-split rows carry the identity the control plane kept, which
        # is the drover#280 shape: one survivor, one collapsed duplicate.
        _seed_legacy(con, ["kept", "dropped"])
        report = prune_legacy_harness_events(con, analytical, apply=True)
    finally:
        con.close()

    assert report["legacy_rows"] == 2
    assert report["missing"] == 0
    assert report["dropped"] is True
    assert not _legacy_table_exists(analytical)


def test_the_prune_reports_without_dropping_unless_asked(tmp_path):
    analytical = tmp_path / "drover.duckdb"
    registry = control_plane_path(analytical)
    _seed_control_plane(registry, "kept", seq=None)

    con = duckdb.connect(str(analytical), read_only=False)
    try:
        _seed_legacy(con, ["kept", "dropped"])
    finally:
        con.close()

    con = duckdb.connect(str(analytical), read_only=True)
    try:
        report = prune_legacy_harness_events(con, analytical, apply=False)
    finally:
        con.close()

    assert report["missing"] == 0
    assert report["dropped"] is False
    assert _legacy_table_exists(analytical)
