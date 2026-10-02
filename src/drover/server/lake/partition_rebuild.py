"""Stream one UTC day at a time and retire each engine at the process boundary."""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict
from pathlib import Path

from .admin_process import run_admin
from .fence import drained_mutation, reader_fence
from .rebuild import SCHEMAS, extract_frozen
from .rebuild_worker import LINEAGE, POLICY_SCHEMA
from .runtime import (
    LakeError,
    LakeSpec,
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


def rebuild_partitioned(source: Path, spec: LakeSpec, *, dry_run=False):
    started = time.monotonic()
    verify_runtime(spec)
    root = spec.data_root.resolve()
    if root.exists():
        raise LakeError("rebuild_requires_new_data_root")
    root.parent.mkdir(parents=True, exist_ok=True)
    root.mkdir(mode=0o700)
    evidence = root / "verification"
    evidence.mkdir()
    inventory = extract_frozen(source, root / "frozen")
    (evidence / "source-files.json").write_text(json.dumps(inventory, indent=2))
    peak = 0

    def run(operation, output, **fields):
        nonlocal peak
        result, rss = run_admin(
            {
                "operation": operation,
                "output": str(output),
                "spill": str(root / "spill"),
                **fields,
            },
            root,
        )
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
        "rss_measurement": "20ms samples: coordinator plus active disposable worker",
    }
    if not dry_run:
        verified, verify_peak = _verify_jobs(spec, report, root / "verify-work")
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


def _verify_jobs(spec, report, work):
    # Retained-file cleanup must not run between the individual day reads.
    with reader_fence(spec.dsn()):
        return _verify_days(spec, report, work)


def _verify_days(spec, report, work):
    peak = 0
    results = {}
    for table in (*SCHEMAS, "agent_events_legacy_metadata"):
        jobs = []
        days = (
            [p["day"] for p in report["days"]]
            if table in {"agent_events", "agent_events_legacy_metadata"}
            else [None]
        )
        for day in days:
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
            result, rss = run_admin(request, spec.data_root)
            peak = max(peak, rss)
            jobs.append(str(out / "hashes.parquet"))
        result, rss = run_admin(
            {
                "operation": "verify_aggregate",
                "output": str(work / table / "aggregate"),
                "spill": str(spec.data_root / "spill"),
                "files": jobs,
            },
            spec.data_root,
        )
        peak = max(peak, rss)
        results[table] = result
    expected = {
        **report["raw"],
        "agent_events": report["canonical_agent_events"],
        "agent_events_legacy_metadata": report["legacy_metadata"],
    }
    if results != expected:
        raise LakeError("lake_verification_mismatch")
    return results, peak


def verify_partitioned(spec):
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
        result, peak = _verify_jobs(spec, report, Path(directory))
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
