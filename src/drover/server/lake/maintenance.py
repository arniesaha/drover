"""Explicit, fenced DuckLake file and snapshot maintenance."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from uuid import uuid4

from .fence import drained_mutation
from .runtime import LakeError, LakeSpec, lake_connection

EXPORT_MAINTENANCE_TABLES = (
    "agent_events",
    "activity_daily",
    "control_outbox_batches",
    "export_event_versions",
    "export_batch_receipts",
)
CLEANUP_RETENTION = "7 days"
MAINTENANCE_AUTHOR = "drover-maintenance"
MAINTENANCE_RECEIPTS = "maintenance-receipts.json"


@dataclass(frozen=True)
class CompactionPolicy:
    """When the hub may spend its idle maintenance budget compacting files."""

    soft_file_limit: int = 5_000
    daily_window_hour: int = 3

    def __post_init__(self) -> None:
        if type(self.soft_file_limit) is not int or self.soft_file_limit < 1:
            raise ValueError("soft_file_limit must be a positive integer")
        if (
            type(self.daily_window_hour) is not int
            or not 0 <= self.daily_window_hour < 24
        ):
            raise ValueError("daily_window_hour must be an hour from 0 through 23")


def compaction_due(
    policy: CompactionPolicy,
    *,
    referenced_files: int,
    last_completed_at: datetime | None,
    now: datetime | None = None,
) -> bool:
    """Run at most once daily, in one quiet UTC hour, or for file pressure."""
    current = (now or datetime.now(UTC)).astimezone(UTC)
    if current.hour != policy.daily_window_hour:
        return False
    if referenced_files > policy.soft_file_limit:
        return True
    if last_completed_at is None:
        return True
    return current - last_completed_at.astimezone(UTC) >= timedelta(days=1)


def compaction_skip_reason(
    *,
    maintenance_dsn_env: str,
    planned_stop_active: bool,
    exporter_backlog_high: bool,
    maintenance_admitted: bool,
) -> str | None:
    """Return the first safety gate refusing a background compaction tick."""
    if not maintenance_dsn_env:
        return "maintenance_dsn_not_configured"
    if planned_stop_active:
        return "planned_stop_active"
    if exporter_backlog_high:
        return "exporter_backlog_high"
    if not maintenance_admitted:
        return "foreground_or_maintenance_active"
    return None


def _file_count(con, table: str) -> int:
    row = con.execute(
        "SELECT coalesce(file_count, 0) FROM ducklake_table_info('lake') "
        "WHERE table_name = ?",
        [table],
    ).fetchone()
    return int(row[0]) if row else 0


def _row_count(con, table: str) -> int:
    return int(con.execute(f'SELECT count(*) FROM lake."{table}"').fetchone()[0])


def _snapshot_count(con) -> int:
    return int(con.execute("SELECT count(*) FROM lake.snapshots()").fetchone()[0])


def _snapshot_rows(con, after=0):
    return [
        list(row)
        for row in con.execute(
            "SELECT snapshot_id,author,commit_message,commit_extra_info "
            "FROM lake.snapshots() WHERE snapshot_id>? ORDER BY snapshot_id",
            [after],
        ).fetchall()
    ]


def _fingerprint(con, table):
    from .rebuild import accounting

    # Export rows created before a full rebuild can have NULL lineage hashes.
    # Hash the whole typed row instead: DuckDB's JSON representation preserves
    # NULL fields, so a missing value is evidence rather than a skipped row.
    return accounting(con, f'lake."{table}" AS t', expression="sha256(to_json(t))")


def _receipt_path(spec):
    return spec.data_root / "verification" / MAINTENANCE_RECEIPTS


def _append_receipt(spec, receipt, *, fence):
    """Publish only a complete, fenced attestation; partial maintenance fails closed."""
    path = _receipt_path(spec)
    try:
        records = json.loads(path.read_text()) if path.exists() else []
        if not isinstance(records, list):
            raise ValueError()
    except (OSError, ValueError):
        raise ValueError("maintenance receipt journal is malformed") from None
    temporary = path.with_suffix(".pending")
    try:
        temporary.write_text(json.dumps([*records, receipt], sort_keys=True))
        fence.check()
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _baseline_snapshot(spec):
    """The oldest snapshot that serving may need; never let expiry cross it."""
    from .serving_proof import _checkpoint, _proof

    try:
        proof, digest = _proof(spec)
    except (OSError, ValueError):
        raise LakeError("lake_verification_required") from None
    checkpoint = _checkpoint(spec.data_root / "verification", digest, proof)
    return checkpoint["snapshot"] if checkpoint else proof["snapshot"], digest, proof


def maintain_lake(
    spec: LakeSpec, *, tables: tuple[str, ...] = EXPORT_MAINTENANCE_TABLES
):
    """Compact one catalog under the drained writer/reader fence.

    Every compaction snapshot is attested with an unchanged logical-table
    fingerprint and then checkpointed. Expiry is capped at the serving
    baseline, so neither the proof nor the current checkpoint can disappear.
    """
    selected = tuple(dict.fromkeys(tables))
    if not selected or any(
        table not in EXPORT_MAINTENANCE_TABLES for table in selected
    ):
        raise ValueError("maintenance tables must be export tables")
    started = monotonic()
    with (
        drained_mutation(spec.dsn()) as fence,
        lake_connection(spec, read_only=False) as con,
    ):
        baseline, proof_digest, proof = _baseline_snapshot(spec)
        snapshots_before = _snapshot_count(con)
        before_snapshot = max(row[0] for row in _snapshot_rows(con))
        from .serving_proof import check_referenced_files

        referenced_files_before = check_referenced_files(spec, con, limit=2**31 - 1)
        before = {table: _fingerprint(con, table) for table in selected}
        before_rows = {table: _row_count(con, table) for table in selected}
        run_id = uuid4().hex
        intent = {
            "version": 1,
            "proof_sha256": proof_digest,
            "catalog_id": proof["catalog_id"],
            "data_root": proof["data_root"],
            "before_snapshot": before_snapshot,
            "tables": before,
        }
        intent_digest = hashlib.sha256(
            json.dumps(intent, sort_keys=True).encode()
        ).hexdigest()
        report = {"tables": {}}
        # Preserve both constraints: DuckLake's usual 30-day time-travel
        # window, and the proof/checkpoint baseline. The earlier cutoff is
        # the only one that cannot remove a younger retained snapshot.
        baseline_timestamp = con.execute(
            "SELECT snapshot_time FROM lake.snapshots() WHERE snapshot_id=?",
            [baseline],
        ).fetchone()[0]
        timestamp = con.execute(
            "SELECT least(?, now() - INTERVAL '30 days')", [baseline_timestamp]
        ).fetchone()[0]
        con.execute("BEGIN")
        try:
            con.execute(
                "CALL lake.set_commit_message(?, ?, extra_info => ?)",
                [MAINTENANCE_AUTHOR, run_id, intent_digest],
            )
            for table in selected:
                before = _file_count(con, table)
                merged = con.execute(
                    "CALL ducklake_merge_adjacent_files('lake', ?, schema => 'main')",
                    [table],
                ).fetchall()
                report["tables"][table] = {
                    "files_before": before,
                    "rows_before": before_rows[table],
                    "merge_outputs": len(merged),
                    "files_after": _file_count(con, table),
                }
                fence.check()
            after = {table: _fingerprint(con, table) for table in selected}
            if after != intent["tables"]:
                raise ValueError("maintenance changed logical table content")
            after_rows = {table: _row_count(con, table) for table in selected}
            if after_rows != before_rows:
                raise ValueError("maintenance changed logical table row counts")
            # Keep every catalog-changing maintenance procedure in this one
            # receipted transaction; a later anonymous expiry snapshot would
            # correctly fail the serving proof.
            con.execute(
                "CALL ducklake_expire_snapshots('lake', older_than => ?)",
                [timestamp],
            )
            fence.check()
            con.execute(
                "CALL ducklake_cleanup_old_files('lake', "
                "older_than => now() - INTERVAL '7 days')"
            )
            fence.check()
            con.execute("COMMIT")
        except BaseException:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass
            raise
        commits = _snapshot_rows(con, before_snapshot)
        if any(
            row[1:] != [MAINTENANCE_AUTHOR, run_id, intent_digest] for row in commits
        ):
            raise ValueError("maintenance produced an unattested snapshot")
        if commits:
            receipt = {
                **intent,
                "after_snapshot": commits[-1][0],
                "commits": commits,
                "after_tables": after,
                "intent_sha256": intent_digest,
            }
            _append_receipt(spec, receipt, fence=fence)
        for table in selected:
            report["tables"][table]["files_after"] = _file_count(con, table)
            report["tables"][table]["rows_after"] = after_rows[table]
        # The serving check validates this receipt before moving its trusted
        # start point. A crash before this step remains deliberately unservable.
        from .serving_proof import advance_checkpoint

        checkpoint = advance_checkpoint(spec, proof_digest, fence=fence, con=con)
        report.update(
            {
                "snapshots_before": snapshots_before,
                "snapshots_after": _snapshot_count(con),
                "referenced_files_before": referenced_files_before,
                "referenced_files_after": check_referenced_files(
                    spec, con, limit=2**31 - 1
                ),
                "cleanup_retention": CLEANUP_RETENTION,
                "baseline_snapshot": baseline,
                "checkpoint_snapshot": checkpoint["snapshot"] if checkpoint else None,
                "elapsed_seconds": round(monotonic() - started, 3),
            }
        )
        return report


def compact_lake(
    spec: LakeSpec, *, tables: tuple[str, ...] = EXPORT_MAINTENANCE_TABLES
):
    """Public name for the row-count-verified DuckLake compaction operation."""
    return maintain_lake(spec, tables=tables)


def referenced_file_health(spec: LakeSpec, *, hard_limit: int) -> dict:
    """Read catalog metadata once for operator-facing file-pressure signals."""
    from .serving_proof import check_referenced_files

    try:
        with lake_connection(spec) as con:
            count = check_referenced_files(spec, con, limit=2**31 - 1)
    except LakeError as exc:
        return {
            "state": "unavailable",
            "detail": exc.code,
            "referenced_files": None,
            "referenced_file_limit": hard_limit,
            "referenced_file_ratio": None,
        }
    except Exception:  # noqa: BLE001 - health must never leak catalog details
        return {
            "state": "unavailable",
            "detail": "lake_health_unavailable",
            "referenced_files": None,
            "referenced_file_limit": hard_limit,
            "referenced_file_ratio": None,
        }
    ratio = count / hard_limit
    state = "degraded" if ratio >= 0.8 else "warn" if ratio >= 0.5 else "ok"
    return {
        "state": state,
        "referenced_files": count,
        "referenced_file_limit": hard_limit,
        "referenced_file_ratio": round(ratio, 6),
    }


def lake_health_for_config(analytics) -> dict | None:
    """One-shot lake health for CLI data-quality output outside the hub."""
    if getattr(analytics, "backend", "legacy") != "ducklake":
        return None
    from .serving import lake_spec
    from .serving_proof import REFERENCED_FILE_LIMIT

    health = referenced_file_health(
        lake_spec(analytics), hard_limit=REFERENCED_FILE_LIMIT
    )
    configured = bool(getattr(analytics, "maintenance_dsn_env", ""))
    health["auto_compaction"] = "enabled" if configured else "disabled"
    if not configured:
        health["detail"] = "auto-compaction disabled (maintenance DSN not configured)"
    return health


def _run_compaction_process(spec: LakeSpec, *, timeout_seconds: float) -> dict:
    """Execute destructive DuckLake maintenance in a killable child process."""
    with tempfile.TemporaryDirectory(prefix="drover-lake-compact-") as directory:
        root = Path(directory)
        request_path = root / "request.json"
        reply_path = root / "reply.json"
        request_path.write_text(
            json.dumps(
                {
                    "catalog_dsn_env": spec.catalog_dsn_env,
                    "data_root": str(spec.data_root),
                    "extension_dir": str(spec.extension_dir),
                    "engine_sha256": spec.engine_sha256,
                    "reply": str(reply_path),
                }
            )
        )
        request_path.chmod(0o600)
        child = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "drover.server.lake.maintenance_worker",
                str(request_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            child.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
            raise LakeError("lake_compaction_deadline_exceeded") from None
        if child.returncode or not reply_path.is_file():
            raise LakeError("lake_compaction_process_failed")
        payload = json.loads(reply_path.read_text())
        if payload.get("error"):
            raise LakeError(str(payload["error"]), payload.get("detail"))
        return payload["report"]


class LakeCompactionScheduler:
    """Low-priority scheduler around the fenced, row-checked compactor."""

    def __init__(
        self,
        *,
        reader_spec: LakeSpec,
        maintenance_dsn_env: str,
        policy: CompactionPolicy,
        max_runtime_seconds: float,
        maintenance_admission,
        planned_stop_active,
        exporter_backlog_high,
        execute=_run_compaction_process,
    ) -> None:
        from .serving_proof import REFERENCED_FILE_LIMIT

        self._reader_spec = reader_spec
        self._maintenance_spec = LakeSpec(
            maintenance_dsn_env,
            reader_spec.data_root,
            reader_spec.extension_dir,
            reader_spec.engine_sha256,
        )
        self._maintenance_dsn_env = maintenance_dsn_env
        self._policy = policy
        self._max_runtime_seconds = max_runtime_seconds
        self._admission = maintenance_admission
        self._planned_stop_active = planned_stop_active
        self._exporter_backlog_high = exporter_backlog_high
        self._execute = execute
        self._hard_limit = REFERENCED_FILE_LIMIT
        self._lock = threading.Lock()
        self._last_completed_at: datetime | None = None
        self._last_report: dict | None = None
        self._last_skip: str | None = None
        self._file_health = {
            "state": "unknown",
            "referenced_files": None,
            "referenced_file_limit": self._hard_limit,
            "referenced_file_ratio": None,
        }

    def refresh_health(self) -> dict:
        health = referenced_file_health(self._reader_spec, hard_limit=self._hard_limit)
        with self._lock:
            self._file_health = health
        return health

    def health(self) -> dict:
        with self._lock:
            last_completed = self._last_completed_at
            report = self._last_report
            skip = self._last_skip
            health = dict(self._file_health)
        health.update(
            {
                "auto_compaction": (
                    "enabled" if self._maintenance_dsn_env else "disabled"
                ),
                "last_completed_at": (
                    last_completed.isoformat() if last_completed else None
                ),
                "last_report": report,
            }
        )
        if not self._maintenance_dsn_env:
            health["detail"] = (
                "auto-compaction disabled (maintenance DSN not configured)"
            )
        elif skip:
            health["last_skip"] = skip
        return health

    def tick(self, *, now: datetime | None = None) -> dict | None:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        health = self.refresh_health()
        count = health.get("referenced_files")
        if not isinstance(count, int) or not compaction_due(
            self._policy,
            referenced_files=count,
            last_completed_at=self._last_completed_at,
            now=current,
        ):
            return None
        planned_stop = bool(self._planned_stop_active())
        backlog = bool(self._exporter_backlog_high())
        with self._admission.admit() as admitted:
            skip = compaction_skip_reason(
                maintenance_dsn_env=self._maintenance_dsn_env,
                planned_stop_active=planned_stop,
                exporter_backlog_high=backlog,
                maintenance_admitted=admitted,
            )
            if skip:
                with self._lock:
                    self._last_skip = skip
                return None
            report = self._execute(
                self._maintenance_spec, timeout_seconds=self._max_runtime_seconds
            )
        with self._lock:
            self._last_completed_at = current
            self._last_report = report
            self._last_skip = None
        return report
