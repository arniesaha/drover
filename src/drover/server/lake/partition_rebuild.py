"""Stream one UTC day at a time and retire each engine at the process boundary."""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict
from pathlib import Path

from .activity_daily import ACTIVITY_DAILY_SCHEMA, refresh_activity_daily
from .admin_process import run_admin
from .fence import drained_mutation, reader_fence
from .rebuild import SCHEMAS, extract_frozen
from .rebuild_worker import LINEAGE, POLICY_SCHEMA
from .runtime import (
    LakeError,
    LakeSpec,
    catalog_permission_preflight,
    configure_catalog,
    create_table,
    lake_connection,
    sha256_file,
    verify_runtime,
)


def _spec(spec):
    return {
        **asdict(spec),
        "data_root": str(spec.data_root),
        "extension_dir": str(spec.extension_dir),
    }


def rebuild_partitioned(
    source: Path, spec: LakeSpec, *, dry_run=False, rss_ceiling=None
):
    started = time.monotonic()
    verify_runtime(spec)
    root = spec.data_root.resolve()
    if root.exists():
        raise LakeError("rebuild_requires_new_data_root")
    if not dry_run:
        # Before the extract and partition phases, which take minutes.
        catalog_permission_preflight(spec)
    root.parent.mkdir(parents=True, exist_ok=True)
    root.mkdir(mode=0o700)
    evidence = root / "verification"
    evidence.mkdir()
    inventory = extract_frozen(source, root / "frozen")
    (evidence / "source-files.json").write_text(json.dumps(inventory, indent=2))
    peak = 0

    def run(operation, output, **fields):
        nonlocal peak
        request = {
            "operation": operation,
            "output": str(output),
            "spill": str(root / "spill"),
            **fields,
        }
        if rss_ceiling is not None:
            result, rss = run_admin(request, root, rss_ceiling=rss_ceiling)
        else:
            result, rss = run_admin(request, root)
        peak = max(peak, rss)
        return result

    days = {}
    for item in inventory["agent_events"]:
        day = next(
            (p[5:] for p in Path(item["path"]).parts if p.startswith("date=")), None
        )
        if day is None or not re.fullmatch(r"(?:\d{4}-\d{2}-\d{2}|_seed)", day):
            raise LakeError("rebuild_missing_day_partition")
        days.setdefault(day, []).append(item)
    partitions = []
    daily = []
    for day, files in sorted(days.items()):
        output = root / "partitions" / day
        daily.append(
            run("events", output, files=files, extracted=str(root / "frozen"), day=day)
        )
        partitions.append(str(output))
    others = {}
    for table in ("provider_usage_snapshots", "control_outbox_batches"):
        outputs = []
        for i in range(0, len(inventory[table]), 32):
            output = root / "partitions" / table / str(i // 32)
            run(
                "other",
                output,
                table=table,
                files=inventory[table][i : i + 32],
                extracted=str(root / "frozen"),
            )
            outputs.append(str(output / "rows.parquet"))
        others[table] = outputs
    aggregate = run(
        "aggregate", root / "aggregate", partitions=partitions, others=others
    )
    raw_rows = sum(p["raw_rows"] for p in daily)
    baseline_rows = sum(p["baseline_canonical_rows"] for p in daily)
    buckets = {
        b: sum(p["buckets"][b] for p in daily)
        for b in ("original", "rebuild_backfill", "legacy_metadata", "legacy_null")
    }
    hashes = aggregate["hashes"]
    serving = hashes["agent_events"]
    archive = hashes["agent_events_legacy_metadata"]
    policy_losers = raw_rows - serving["rows"] - archive["rows"]
    if sum(buckets.values()) != raw_rows or policy_losers < raw_rows - baseline_rows:
        raise LakeError("rebuild_bucket_accounting_mismatch")
    tables = {
        "agent_events": [str(Path(p) / "events.parquet") for p in partitions],
        "agent_events_legacy_metadata": [
            str(Path(p) / "metadata.parquet") for p in partitions
        ],
        **others,
    }
    if not dry_run:
        with drained_mutation(spec.dsn()) as fence:
            if fence.connection.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog','information_schema')"
            ).fetchone()[0]:
                raise LakeError("rebuild_requires_fresh_catalog")
            with lake_connection(spec, read_only=False, create=True) as con:
                configure_catalog(con)
                for table in tables:
                    schema = (
                        POLICY_SCHEMA
                        if table in {"agent_events", "agent_events_legacy_metadata"}
                        else SCHEMAS[table]
                    )
                    create_table(
                        con,
                        table,
                        schema | LINEAGE,
                        day_partition=table
                        in {"agent_events", "agent_events_legacy_metadata"},
                    )
                create_table(
                    con, "activity_daily", ACTIVITY_DAILY_SCHEMA, day_partition=True
                )
            for index, partition in enumerate(partitions):
                fence.check()
                run(
                    "publish",
                    root / "publication" / str(index),
                    spec=_spec(spec),
                    tables={
                        t: [str(Path(partition) / f)]
                        for t, f in (
                            ("agent_events", "events.parquet"),
                            ("agent_events_legacy_metadata", "metadata.parquet"),
                        )
                    },
                )
            for table, files in others.items():
                for index, path in enumerate(files):
                    fence.check()
                    run(
                        "publish",
                        root / "publication" / table / str(index),
                        spec=_spec(spec),
                        tables={table: [path]},
                    )
            # The offline rebuild is not an outbox export, but it must leave
            # the selected lake immediately queryable through the same compact
            # read model. This is one derived snapshot after all immutable
            # frozen partitions are visible.
            with lake_connection(spec, read_only=False) as con:
                refresh_activity_daily(
                    con,
                    [item["day"] for item in daily],
                )
    report = {
        "format_version": 2,
        "dry_run": dry_run,
        "raw": {
            "agent_events": {
                "rows": raw_rows,
                "normalized_multiset_sha256": hashes["raw"],
            },
            **aggregate["others"],
        },
        "baseline_canonical_agent_events": {
            "rows": baseline_rows,
            "normalized_multiset_sha256": hashes["baseline"],
        },
        "canonical_agent_events": serving,
        "legacy_metadata": archive,
        "buckets": buckets,
        "losers": policy_losers,
        "original_key_losers": raw_rows - baseline_rows,
        "backfill_additional_losers": policy_losers - (raw_rows - baseline_rows),
        "null_key_rows_retained": buckets["legacy_null"],
        "cross_partition_check": aggregate["cross_partition_check"],
        "days": daily,
        "spill_directory": str(root / "spill"),
        "elapsed_seconds": time.monotonic() - started,
        "peak_rss_bytes": peak,
        "rss_ceiling_bytes": (
            rss_ceiling if rss_ceiling is not None else 5 * 1024**3 // 2
        ),
        "rss_measurement": "20ms samples: coordinator plus active disposable worker",
    }
    if not dry_run:
        verified, verify_peak = _verify_jobs(
            spec, report, root / "verify-work", rss_ceiling=rss_ceiling
        )
        peak = max(peak, verify_peak)
        report["verification"] = verified
        report["peak_rss_bytes"] = peak
        report["elapsed_seconds"] = time.monotonic() - started
    report["evidence_sha256"] = {
        p.name: sha256_file(p) for p in evidence.iterdir() if p.is_file()
    }
    # All row-output and mapping files are covered by immutable evidence hashes.
    report["partition_files_sha256"] = {
        str(p.relative_to(root)): sha256_file(p)
        for p in sorted((root / "partitions").rglob("*.parquet"))
    }
    report["elapsed_seconds"] = time.monotonic() - started
    (evidence / "report.json").write_text(json.dumps(report, indent=2))
    return report


def _verify_jobs(spec, report, work, *, rss_ceiling=None, fence_held=False):
    # Retained-file cleanup must not run between the individual day reads.
    # A drained mutation already owns the exclusive reader lock, so taking a
    # second shared lock on another PG connection would self-deadlock.
    if fence_held:
        return _verify_days(
            spec, report, work, rss_ceiling=rss_ceiling, fence_held=True
        )
    with reader_fence(spec.dsn()):
        return _verify_days(spec, report, work, rss_ceiling=rss_ceiling)


def _verify_days(spec, report, work, *, rss_ceiling=None, fence_held=False):
    peak = 0
    from .query_process import query

    def metadata_rows(statement):
        # A drained mutation excludes every reader.  Its own verify must not
        # spawn a child that tries to acquire that lock, but it can safely read
        # through the already-fenced administrator process.
        if fence_held:
            with lake_connection(spec) as con:
                return con.execute(statement).fetchall()
        return query(spec, statement)["rows"]

    tables = {
        row[0]
        for row in metadata_rows(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_catalog='lake' AND table_schema='main'"
        )
    }
    export_tables = {"export_batch_receipts", "export_event_versions"}
    if export_tables & tables and not export_tables <= tables:
        raise LakeError("lake_verification_receipt_tables_missing")
    has_exports = export_tables <= tables

    def run(request):
        nonlocal peak
        if rss_ceiling is not None:
            result, rss = run_admin(request, spec.data_root, rss_ceiling=rss_ceiling)
        else:
            result, rss = run_admin(request, spec.data_root)
        peak = max(peak, rss)
        return result

    if has_exports:
        # This is the same export snapshot/receipt commitment serving trusts.
        # Unlike a serving proof, it intentionally allows a verify to certify
        # an intervening exporter-provisioning snapshot after its frozen rows
        # have independently been checked below.
        from .serving_proof import validate_export_receipts

        with lake_connection(spec) as con:
            try:
                validate_export_receipts(con)
            except Exception:
                raise LakeError("lake_verification_required") from None
        run(
            {
                "operation": "verify_receipts",
                "spec": _spec(spec),
                "output": str(work / "receipts"),
                "spill": str(spec.data_root / "spill"),
            }
        )

    baseline_days = {item["day"] for item in report["days"]}
    event_days = {
        row[0] for row in metadata_rows("SELECT DISTINCT date FROM lake.agent_events")
    }
    if has_exports:
        event_days |= {
            row[0]
            for row in metadata_rows(
                "SELECT DISTINCT date FROM lake.export_event_versions"
            )
        }
    legacy_days = {
        row[0]
        for row in metadata_rows(
            "SELECT DISTINCT date FROM lake.agent_events_legacy_metadata"
        )
    }
    if None in event_days or None in legacy_days:
        raise LakeError("lake_verification_unexpected_partition")
    partition_files = {
        day: {
            "agent_events": [
                str(spec.data_root / "partitions" / day / "events.parquet")
            ],
            "agent_events_legacy_metadata": [
                str(spec.data_root / "partitions" / day / "metadata.parquet")
            ],
        }
        for day in baseline_days
    }
    for table, days in (
        ("agent_events", event_days | baseline_days),
        ("agent_events_legacy_metadata", legacy_days | baseline_days),
    ):
        for day in sorted(days):
            run(
                {
                    "operation": "verify_baseline",
                    "spec": _spec(spec),
                    "output": str(work / table / day),
                    "spill": str(spec.data_root / "spill"),
                    "table": table,
                    "day": day,
                    "files": partition_files.get(day, {}).get(table, []),
                    "has_exports": has_exports,
                }
            )
    control_files = sorted(
        str(path)
        for path in (spec.data_root / "partitions" / "control_outbox_batches").glob(
            "*/rows.parquet"
        )
    )
    run(
        {
            "operation": "verify_baseline",
            "spec": _spec(spec),
            "output": str(work / "control_outbox_batches"),
            "spill": str(spec.data_root / "spill"),
            "table": "control_outbox_batches",
            "files": control_files,
            "has_exports": has_exports,
        }
    )

    results = {}
    # Provider observations are not exporter output and retain their original
    # whole-table identity. The other three frozen tables were checked above
    # using a baseline-plus-receipt rule, so their report fingerprints remain
    # the stable evidence stored in the serving proof.
    for table in ("provider_usage_snapshots",):
        jobs = []
        for day in [None]:
            out = work / table / (day or "all")
            out.mkdir(parents=True, exist_ok=True)
            request = {
                "operation": "verify",
                "spec": _spec(spec),
                "output": str(out),
                "table": table,
                "spill": str(spec.data_root / "spill"),
            }
            if day is not None:
                request["day"] = day
            run(request)
            jobs.append(str(out / "hashes.parquet"))
        request = {
            "operation": "verify_aggregate",
            "output": str(work / table / "aggregate"),
            "spill": str(spec.data_root / "spill"),
            "files": jobs,
        }
        results[table] = run(request)
    expected = {
        **report["raw"],
        "agent_events": report["canonical_agent_events"],
        "agent_events_legacy_metadata": report["legacy_metadata"],
    }
    if results["provider_usage_snapshots"] != expected["provider_usage_snapshots"]:
        raise LakeError("lake_verification_mismatch")
    return expected, peak


def _verify_partitioned_fenced(spec, fence):
    """Verify and publish a proof while the caller retains the writer fence."""
    from .runtime import lake_connection
    from .serving_proof import write_proof

    with lake_connection(spec) as con:
        snapshot = con.execute(
            "SELECT max(snapshot_id) FROM lake.snapshots()"
        ).fetchone()[0]
    fence.check()
    result = _verify_partitioned(spec, fence_held=True)
    fence.check()
    with lake_connection(spec) as con:
        if (
            con.execute("SELECT max(snapshot_id) FROM lake.snapshots()").fetchone()[0]
            != snapshot
        ):
            raise LakeError("lake_verification_snapshot_changed")
        write_proof(spec, con, result, fence=fence, snapshot=snapshot)
    return result


def verify_partitioned(spec, *, fence=None):
    from .fence import MutationFence

    if fence is not None:
        return _verify_partitioned_fenced(spec, fence)
    with MutationFence(spec.dsn()) as fence:
        return _verify_partitioned_fenced(spec, fence)


def _verify_partitioned(spec, *, fence_held=False):
    started = time.monotonic()
    report_path = spec.data_root / "verification/report.json"
    if not report_path.is_file():
        raise LakeError("lake_verification_baseline_missing")
    report = json.loads(report_path.read_text())
    if report["dry_run"]:
        raise LakeError("lake_dry_run_not_published")
    for name, digest in report["evidence_sha256"].items():
        if Path(name).name != name or sha256_file(report_path.parent / name) != digest:
            raise LakeError("lake_verification_evidence_mismatch")
    for name, digest in report["partition_files_sha256"].items():
        path = spec.data_root / name
        if (
            not path.resolve().is_relative_to(spec.data_root.resolve())
            or sha256_file(path) != digest
        ):
            raise LakeError("lake_verification_evidence_mismatch")
    # A fresh disposable engine for every day prevents allocator retention.
    import tempfile

    with tempfile.TemporaryDirectory(prefix="verify-", dir=spec.data_root) as directory:
        result, peak = _verify_jobs(
            spec, report, Path(directory), fence_held=fence_held
        )
    (spec.data_root / "verification/last-verify.json").write_text(
        json.dumps(
            {
                "elapsed_seconds": time.monotonic() - started,
                "peak_rss_bytes": peak,
                "tables": result,
            },
            indent=2,
        )
    )
    return result
