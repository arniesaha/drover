"""Fenced retirement of unreceipted rows written by the retired importer."""

from __future__ import annotations

import hashlib
import json
from time import monotonic
from uuid import uuid4

from .activity_daily import refresh_activity_daily
from .admin_process import run_admin
from .fence import drained_mutation
from .maintenance import MAINTENANCE_AUTHOR, _baseline_snapshot
from .partition_rebuild import verify_partitioned
from .rebuild import literal_list
from .runtime import LakeError, LakeSpec, lake_connection

DEPRECATION_RECORDS = "legacy-deprecations.json"
LEGACY_PREFIX = "legacy-import:%"
SELECTION_RULE = (
    "legacy agent_events in excess of export_event_versions per (date,_row_sha256); "
    "all legacy rows in non-export tables; import_watermark becomes earliest retained day"
)


def _spec(spec: LakeSpec) -> dict:
    return {
        "catalog_dsn_env": spec.catalog_dsn_env,
        "data_root": str(spec.data_root),
        "extension_dir": str(spec.extension_dir),
        "engine_sha256": spec.engine_sha256,
    }


def _table_names(con) -> set[str]:
    return {
        row[0]
        for row in con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_catalog='lake' AND table_schema='main'"
        ).fetchall()
    }


def _per_day(con, sql: str, parameters=()) -> list[dict]:
    return [
        {"day": row[0] or "<unknown>", "rows": int(row[1])}
        for row in con.execute(sql, parameters).fetchall()
    ]


def _agent_event_selection(con) -> list[dict]:
    """Select surplus legacy copies, reserving exporter rows before legacy ones."""
    days = [
        row[0]
        for row in con.execute(
            "SELECT DISTINCT date FROM lake.agent_events "
            "WHERE _import_file LIKE ? ORDER BY 1",
            [LEGACY_PREFIX],
        ).fetchall()
    ]
    selected = []
    for day in days:
        rows = _per_day(
            con,
            """WITH versions AS (
             SELECT date,_row_sha256,count(*) AS rows
             FROM lake.export_event_versions WHERE date=? GROUP BY 1,2
           ), nonlegacy AS (
             SELECT date,_row_sha256,count(*) AS rows
             FROM lake.agent_events
             WHERE date=? AND (_import_file NOT LIKE ? OR _import_file IS NULL)
             GROUP BY 1,2
           ), numbered AS (
             SELECT event._import_file,event._import_row,event.date,event._row_sha256,
               row_number() OVER (
                 PARTITION BY event.date,event._row_sha256
                 ORDER BY event._import_file,event._import_row
               ) AS ordinal,
               greatest(coalesce(versions.rows,0)-coalesce(nonlegacy.rows,0),0)
                 AS legacy_rows
             FROM lake.agent_events event
             LEFT JOIN versions USING (date,_row_sha256)
             LEFT JOIN nonlegacy USING (date,_row_sha256)
             WHERE event.date=? AND event._import_file LIKE ?
           )
           SELECT date,count(*) FROM numbered WHERE ordinal > legacy_rows GROUP BY 1""",
            [day, day, LEGACY_PREFIX, day, LEGACY_PREFIX],
        )
        selected.extend(rows)
    return selected


def _selection_plan(con) -> dict:
    """Return only rows whose legacy provenance is not independently attested."""
    names = _table_names(con)
    tables: dict[str, list[dict]] = {}
    affected_days: set[str] = set()

    def add(name: str, sql: str, parameters=(), *, affects_activity=True):
        if name not in names:
            tables[name] = []
            return
        rows = _per_day(con, sql, parameters)
        tables[name] = rows
        if affects_activity:
            affected_days.update(
                row["day"] for row in rows if row["day"] != "<unknown>"
            )

    tables["agent_events"] = (
        _agent_event_selection(con) if "agent_events" in names else []
    )
    affected_days.update(
        row["day"] for row in tables["agent_events"] if row["day"] != "<unknown>"
    )
    # Outbox rows have no compatible version identity. A legacy-import row is
    # therefore not an exporter receipt row and is always deprecated.
    add(
        "control_outbox_batches",
        """SELECT CAST(created_at AS DATE)::VARCHAR, count(*)
           FROM lake.control_outbox_batches row
           WHERE row._import_file LIKE ? GROUP BY 1 ORDER BY 1""",
        [LEGACY_PREFIX],
    )
    # Provider observations are not exporter output, so no receipt/version can
    # establish provenance for a legacy-import row.
    add(
        "provider_usage_snapshots",
        """SELECT CAST(observed_at AS DATE)::VARCHAR, count(*)
           FROM lake.provider_usage_snapshots
           WHERE _import_file LIKE ? GROUP BY 1 ORDER BY 1""",
        [LEGACY_PREFIX],
    )
    # This table is solely the retired importer's cursor. Replace a stale
    # cursor with the retained lake boundary so historical windows remain
    # visibly archived after the importer is gone.
    add(
        "import_watermark",
        """SELECT partition_date, count(*) FROM lake.import_watermark
           WHERE partition_date IS DISTINCT FROM (
             SELECT min(date) FROM lake.agent_events WHERE date <> '_seed'
           ) GROUP BY 1 ORDER BY 1""",
        affects_activity=False,
    )
    return {
        "selection_rule": SELECTION_RULE,
        "tables": tables,
        "affected_days": sorted(affected_days),
        "rows": sum(row["rows"] for rows in tables.values() for row in rows),
    }


def deprecation_plan(spec: LakeSpec) -> dict:
    """Read-only inventory for the default operator dry-run."""
    result, peak = run_admin(
        {
            "operation": "deprecate_legacy",
            "spec": _spec(spec),
            "apply": False,
            "output": str(spec.data_root / "deprecation-plan"),
            "spill": str(spec.data_root / "spill"),
        },
        spec.data_root,
    )
    plan = result["plan"]
    return {"dry_run": True, **plan, "peak_rss_bytes": peak}


def _append_deprecation_record(spec, record, *, fence) -> None:
    path = spec.data_root / "verification" / DEPRECATION_RECORDS
    try:
        records = json.loads(path.read_text()) if path.exists() else []
        if not isinstance(records, list):
            raise ValueError()
    except (OSError, ValueError):
        raise LakeError("lake_deprecation_evidence_malformed") from None
    temporary = path.with_suffix(".pending")
    try:
        temporary.write_text(json.dumps([*records, record], sort_keys=True, indent=2))
        fence.check()
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _delete_selected(con, names: set[str], agent_days: list[str]) -> None:
    if "agent_events" in names and agent_days:
        # Rows without a same-day version cannot consume receipt capacity, so
        # discard them in one bounded anti-join.  Only the exceptional
        # version-overlap days need the expensive ordinal accounting below.
        # This keeps the writer's allocator from retaining one window state
        # per historical day while preserving count-aware semantics.
        con.execute(
            """DELETE FROM lake.agent_events event
               WHERE event._import_file LIKE ? AND NOT EXISTS (
                 SELECT 1 FROM lake.export_event_versions version
                 WHERE version.date=event.date
                   AND version._row_sha256=event._row_sha256
               )""",
            [LEGACY_PREFIX],
        )
        overlap_days = [
            row[0]
            for row in con.execute(
                """SELECT DISTINCT event.date FROM lake.agent_events event
                   JOIN lake.export_event_versions version
                     USING (date,_row_sha256)
                   WHERE event._import_file LIKE ?
                     AND event.date IN """ + literal_list(agent_days),
                [LEGACY_PREFIX],
            ).fetchall()
        ]
        for day in overlap_days:
            con.execute(
                """DELETE FROM lake.agent_events event WHERE EXISTS (
                 WITH versions AS (
                   SELECT date,_row_sha256,count(*) AS rows
                   FROM lake.export_event_versions WHERE date=? GROUP BY 1,2
                 ), nonlegacy AS (
                   SELECT date,_row_sha256,count(*) AS rows
                   FROM lake.agent_events
                   WHERE date=? AND (_import_file NOT LIKE ? OR _import_file IS NULL)
                   GROUP BY 1,2
                 ), numbered AS (
                   SELECT candidate._import_file,candidate._import_row,candidate.date,
                     candidate._row_sha256,
                     row_number() OVER (
                       PARTITION BY candidate.date,candidate._row_sha256
                       ORDER BY candidate._import_file,candidate._import_row
                     ) AS ordinal,
                     greatest(coalesce(versions.rows,0)-coalesce(nonlegacy.rows,0),0)
                       AS legacy_rows
                   FROM lake.agent_events candidate
                   LEFT JOIN versions USING (date,_row_sha256)
                   LEFT JOIN nonlegacy USING (date,_row_sha256)
                   WHERE candidate.date=? AND candidate._import_file LIKE ?
                 )
                 SELECT 1 FROM numbered candidate
                 WHERE candidate.ordinal > candidate.legacy_rows
                   AND event._import_file IS NOT DISTINCT FROM candidate._import_file
                   AND event._import_row IS NOT DISTINCT FROM candidate._import_row
                   AND event.date IS NOT DISTINCT FROM candidate.date
                   AND event._row_sha256 IS NOT DISTINCT FROM candidate._row_sha256
               )""",
                [day, day, LEGACY_PREFIX, day, LEGACY_PREFIX],
            )
    if "control_outbox_batches" in names:
        con.execute(
            "DELETE FROM lake.control_outbox_batches WHERE _import_file LIKE ?",
            [LEGACY_PREFIX],
        )
    if "provider_usage_snapshots" in names:
        con.execute(
            "DELETE FROM lake.provider_usage_snapshots WHERE _import_file LIKE ?",
            [LEGACY_PREFIX],
        )
    if "import_watermark" in names:
        con.execute("DELETE FROM lake.import_watermark")


def _write_retained_watermark(con, names: set[str]) -> None:
    """Retain one archive boundary after retiring the legacy cursor."""
    if "import_watermark" not in names:
        return
    day = con.execute(
        "SELECT min(date) FROM lake.agent_events WHERE date <> '_seed'"
    ).fetchone()[0]
    if day is None:
        return
    con.execute("INSERT INTO lake.import_watermark VALUES (?)", [day])


def _precommit_verify_agent_events(spec: LakeSpec, con) -> None:
    """Run #637's baseline-plus-version count invariant before committing."""
    try:
        report = json.loads(
            (spec.data_root / "verification" / "report.json").read_text()
        )
        baseline_files = {
            day["day"]: str(
                spec.data_root / "partitions" / day["day"] / "events.parquet"
            )
            for day in report["days"]
        }
    except (OSError, ValueError, KeyError, TypeError):
        raise LakeError("lake_deprecation_would_fail_verify") from None
    if (
        con.execute(
            "SELECT 1 FROM lake.agent_events WHERE date IS NULL LIMIT 1"
        ).fetchone()
        or con.execute(
            "SELECT 1 FROM lake.export_event_versions WHERE date IS NULL LIMIT 1"
        ).fetchone()
    ):
        raise LakeError("lake_deprecation_would_fail_verify")
    days = {
        row[0]
        for row in con.execute("SELECT DISTINCT date FROM lake.agent_events").fetchall()
    } | {
        row[0]
        for row in con.execute(
            "SELECT DISTINCT date FROM lake.export_event_versions"
        ).fetchall()
    }
    try:
        for day in sorted(days):
            baseline = baseline_files.get(day)
            if baseline:
                baseline_sql = (
                    "SELECT _row_sha256 FROM read_parquet("
                    + literal_list([baseline])
                    + ")"
                )
            else:
                baseline_sql = "SELECT _row_sha256 FROM lake.agent_events WHERE false"
            unexpected = con.execute(
                """WITH allowed AS (
                     SELECT _row_sha256,count(*) AS rows FROM ("""
                + baseline_sql
                + """ UNION ALL
                     SELECT _row_sha256 FROM lake.export_event_versions WHERE date=?
                     ) GROUP BY 1
                   ), actual AS (
                     SELECT _row_sha256,count(*) AS rows FROM lake.agent_events
                     WHERE date=? GROUP BY 1
                   )
                   SELECT 1 FROM actual LEFT JOIN allowed USING (_row_sha256)
                   WHERE actual.rows > coalesce(allowed.rows,0) LIMIT 1""",
                [day, day],
            ).fetchone()
            if unexpected:
                raise LakeError("lake_deprecation_would_fail_verify")
    except Exception:
        raise LakeError("lake_deprecation_would_fail_verify") from None


def _deprecate_legacy_inprocess(spec: LakeSpec, *, apply: bool = False) -> dict:
    """Deprecate legacy-import rows, then recertify the resulting lake.

    The operation intentionally does not accept an allowlist or a trusted
    legacy mode: a row is retained only when existing exporter evidence proves
    it is an exporter version.
    """
    if not apply:
        return deprecation_plan(spec)
    started = monotonic()
    with drained_mutation(spec.dsn()) as fence:
        with lake_connection(
            spec,
            read_only=False,
            settings={
                "memory_limit": "512MB",
                "temp_directory": str(spec.data_root / "spill"),
            },
        ) as con:
            plan = _selection_plan(con)
            if plan["rows"] == 0:
                return {
                    "dry_run": False,
                    "no_op": True,
                    **plan,
                    "elapsed_seconds": round(monotonic() - started, 3),
                }
            proof_snapshot, proof_digest, proof = _baseline_snapshot(spec)
            before_snapshot = con.execute(
                "SELECT max(snapshot_id) FROM lake.snapshots()"
            ).fetchone()[0]
            run_id = uuid4().hex
            intent = {
                "version": 1,
                "operation": "deprecate-legacy",
                "proof_sha256": proof_digest,
                "catalog_id": proof["catalog_id"],
                "data_root": proof["data_root"],
                "before_snapshot": before_snapshot,
                "selection_rule": SELECTION_RULE,
                "counts": plan["tables"],
            }
            intent_digest = hashlib.sha256(
                json.dumps(intent, sort_keys=True).encode()
            ).hexdigest()
            con.execute("BEGIN")
            try:
                con.execute(
                    "CALL lake.set_commit_message(?, ?, extra_info => ?)",
                    [MAINTENANCE_AUTHOR, run_id, intent_digest],
                )
                _delete_selected(con, _table_names(con), plan["affected_days"])
                refresh_activity_daily(con, plan["affected_days"])
                _write_retained_watermark(con, _table_names(con))
                _precommit_verify_agent_events(spec, con)
                fence.check()
                con.execute("COMMIT")
            except BaseException:
                try:
                    con.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            after_snapshot = con.execute(
                "SELECT max(snapshot_id) FROM lake.snapshots()"
            ).fetchone()[0]
        if after_snapshot == before_snapshot:
            raise LakeError("lake_deprecation_no_snapshot")
        record = {
            **intent,
            "run_id": run_id,
            "proof_snapshot": proof_snapshot,
            "after_snapshot": after_snapshot,
            "affected_days": plan["affected_days"],
        }
        _append_deprecation_record(spec, record, fence=fence)
        # Keep the drain through the expensive check: no anonymous snapshot can
        # land between deletion, verification, and proof publication.
        verification = verify_partitioned(spec, fence=fence)
        proof_after = hashlib.sha256(
            (spec.data_root / "verification" / "serving-proof.json").read_bytes()
        ).hexdigest()
        from .serving_proof import advance_checkpoint, check_proof

        with lake_connection(spec, read_only=False) as con:
            serving = check_proof(spec, con, proof_after)
            checkpoint = advance_checkpoint(spec, proof_after, fence=fence, con=con)
        return {
            "dry_run": False,
            "no_op": False,
            **plan,
            "before_snapshot": before_snapshot,
            "after_snapshot": after_snapshot,
            "proof_sha256": proof_after,
            "verification": verification,
            "serving_snapshot": serving["snapshot"],
            "checkpoint_snapshot": checkpoint["snapshot"] if checkpoint else None,
            "elapsed_seconds": round(monotonic() - started, 3),
        }


def deprecate_legacy(spec: LakeSpec, *, apply: bool = False) -> dict:
    """Run mutation work in a disposable engine while retaining the fence."""
    if not apply:
        return deprecation_plan(spec)
    started = monotonic()
    with drained_mutation(spec.dsn()) as fence:
        proof_snapshot, proof_digest, proof = _baseline_snapshot(spec)
        run_id = uuid4().hex
        result, child_rss = run_admin(
            {
                "operation": "deprecate_legacy",
                "spec": _spec(spec),
                "apply": True,
                "output": str(spec.data_root / "deprecation-mutation"),
                "spill": str(spec.data_root / "spill"),
                "run_id": run_id,
                "author": MAINTENANCE_AUTHOR,
                "intent": {
                    "version": 1,
                    "operation": "deprecate-legacy",
                    "proof_sha256": proof_digest,
                    "catalog_id": proof["catalog_id"],
                    "data_root": proof["data_root"],
                    "selection_rule": SELECTION_RULE,
                },
            },
            spec.data_root,
        )
        fence.check()
        plan = result["plan"]
        if result["no_op"]:
            return {
                "dry_run": False,
                "no_op": True,
                **plan,
                "child_peak_rss_bytes": child_rss,
                "elapsed_seconds": round(monotonic() - started, 3),
            }
        if result["after_snapshot"] == result["before_snapshot"]:
            raise LakeError("lake_deprecation_no_snapshot")
        record = {
            **result["intent"],
            "run_id": run_id,
            "proof_snapshot": proof_snapshot,
            "after_snapshot": result["after_snapshot"],
            "affected_days": plan["affected_days"],
        }
        _append_deprecation_record(spec, record, fence=fence)
        verification = verify_partitioned(spec, fence=fence)
        proof_after = hashlib.sha256(
            (spec.data_root / "verification" / "serving-proof.json").read_bytes()
        ).hexdigest()
        from .serving_proof import advance_checkpoint, check_proof

        with lake_connection(spec, read_only=False) as con:
            serving = check_proof(spec, con, proof_after)
            checkpoint = advance_checkpoint(spec, proof_after, fence=fence, con=con)
        return {
            "dry_run": False,
            "no_op": False,
            **plan,
            "before_snapshot": result["before_snapshot"],
            "after_snapshot": result["after_snapshot"],
            "proof_sha256": proof_after,
            "verification": verification,
            "serving_snapshot": serving["snapshot"],
            "checkpoint_snapshot": checkpoint["snapshot"] if checkpoint else None,
            "child_peak_rss_bytes": child_rss,
            "elapsed_seconds": round(monotonic() - started, 3),
        }
