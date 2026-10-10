"""Explicitly owned DuckLake exporter; not enabled by legacy server startup."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from psycopg.conninfo import make_conninfo

from drover.server.control_outbox import (
    OutboxClaim,
    _claim_rows,
    _validate_claim_for_publication,
    canonical_payload,
    claim_outbox_batch,
    export_projection,
    payload_sha256,
)
from drover.server.control_store import is_postgres_control_store
from drover.server.db import control_plane_connection
from drover.server.memory_identity import project_control_event
from drover.task_id import compute_task_id

from .activity_daily import ACTIVITY_DAILY_SCHEMA, refresh_activity_daily
from .export_guard import APPLICATION_PREFIX, activate, install_commit_guard
from .export_worker import RECEIPT_SCHEMA, receipt_hash, validate_input
from .fence import MutationFence
from .query_process import _rss
from .rebuild_worker import LINEAGE, POLICY_SCHEMA
from .runtime import LakeError, LakeSpec, create_table, lake_connection, verify_runtime

MAX_INPUT_BYTES = 32 * 1024**2


def provision_exporter(spec: LakeSpec, *, exporter_role: str | None = None):
    """Admin-only setup on an initialized catalog; startup does no lake DDL."""
    with MutationFence(spec.dsn()), lake_connection(spec, read_only=False) as con:
        names = {
            r[0]
            for r in con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_catalog='lake'"
            ).fetchall()
        }
        if not {"agent_events", "control_outbox_batches"} <= names:
            raise LakeError("lake_export_tables_missing")
        if {"export_batch_receipts", "export_event_versions"} & names:
            raise LakeError("lake_export_already_provisioned")
        if "activity_daily" not in names:
            create_table(
                con, "activity_daily", ACTIVITY_DAILY_SCHEMA, day_partition=True
            )
            refresh_activity_daily(
                con,
                [
                    row[0]
                    for row in con.execute(
                        "SELECT DISTINCT date FROM lake.agent_events"
                    ).fetchall()
                ],
            )
        create_table(con, "export_batch_receipts", RECEIPT_SCHEMA)
        create_table(
            con, "export_event_versions", POLICY_SCHEMA | LINEAGE, day_partition=True
        )
    install_commit_guard(spec, exporter_role=exporter_role)


def _json_value(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    raise TypeError(type(value).__name__)


def _validate_source(document, rows):
    validate_input(document)
    normalized = json.loads(json.dumps(rows, default=_json_value))
    if document["raw_rows"] != normalized:
        raise LakeError("lake_export_source_changed")


class LakeOutboxExporter:
    """Hold one dedicated catalog PG advisory lock for the entire owner lifetime.

    Work is synchronous and bounded, with one isolated engine per batch. Only
    the backend-selected hub lifecycle constructs it.
    """

    def __init__(
        self, *, control_path: Path, spec: LakeSpec, batch_size=100, owner=None
    ):
        if not is_postgres_control_store(control_path):
            raise ValueError("DuckLake export requires a PostgreSQL control store")
        if not 1 <= batch_size <= 1000:
            raise ValueError("batch_size must be between 1 and 1000")
        self.control_path = Path(control_path)
        self.spec = spec
        self.batch_size = batch_size
        self.owner = owner or "lake-exporter-" + uuid4().hex
        self.fence = None
        self._batch_lock = threading.Lock()
        self._cancelled = threading.Event()
        self._cancel_lock = threading.Lock()
        self._control_connection = None
        self.catalog_id = None
        self.token = None

    def __enter__(self):
        if self.fence is not None:
            raise LakeError("lake_exporter_already_owned")
        verify_runtime(self.spec)
        fence = MutationFence(self.spec.dsn())
        fence.__enter__()
        try:
            self.catalog_id, self.token = activate(fence)
        except BaseException:
            fence.__exit__(None, None, None)
            raise
        self.fence = fence
        return self

    def __exit__(self, *exc):
        with self._cancel_lock:
            if self.fence:
                self.fence.__exit__(*exc)
            self.fence = None
            self.token = None

    @property
    def cancelled(self):
        return self._cancelled.is_set()

    def cancel(self):
        """Cooperatively unwind the owner, never release its fence remotely.

        libpq cancellation is thread-safe. Hold registration until it completes
        so a returned pool connection cannot cancel a different borrower's work.
        The publisher's next check kills/reaps its isolated child in finally.
        """
        self._cancelled.set()
        with self._cancel_lock:
            connections = [self._control_connection]
            if self.fence is not None:
                connections.append(self.fence.connection)
            for connection in connections:
                if connection is not None:
                    try:
                        connection.cancel_safe(timeout=1)
                    except Exception:
                        # Cancellation is best effort. The lifecycle never
                        # replaces an owner that has not exited and fenced out.
                        pass

    @contextmanager
    def _control(self):
        with control_plane_connection(self.control_path, timeout=2) as control:
            with self._cancel_lock:
                self._control_connection = control._connection
            try:
                self._check()
                yield control
            finally:
                with self._cancel_lock:
                    self._control_connection = None

    def _check(self):
        if self.cancelled:
            raise LakeError("lake_export_recovery_deadline")
        if self.fence is None:
            raise LakeError("lake_exporter_not_owned")
        self.fence.check()

    def _pending(self, control):
        # Replacement owns the sole fence, so it can recover a durable frozen
        # batch immediately, even if the former PG outbox lease has not expired.
        row = control.execute(
            """SELECT x.batch_id,x.input_json,x.input_sha256,x.catalog_id
            FROM lake_export_batches x JOIN control_outbox_batches b USING (batch_id)
            WHERE x.acknowledged_at IS NULL ORDER BY b.created_at,b.batch_id LIMIT 1"""
        ).fetchone()
        if not row:
            return None
        if row[3] != self.catalog_id or payload_sha256(row[1]) != row[2]:
            raise LakeError("lake_export_input_mismatch")
        document = json.loads(row[1])
        claim = OutboxClaim(
            row[0], tuple(document["event_ids"]), self.owner, datetime.now(timezone.utc)
        )
        _validate_source(document, _claim_rows(control, claim))
        return document

    def _freeze(self, control, claim):
        _validate_claim_for_publication(control, claim)
        rows = _claim_rows(control, claim)
        if tuple(r["event_id"] for r in rows) != claim.event_ids:
            raise LakeError("lake_export_membership_mismatch")
        events = export_projection(control, rows, projector=project_control_event)
        document = json.loads(
            json.dumps(
                {
                    "contract_version": 1,
                    "catalog_id": self.catalog_id,
                    "batch_id": claim.batch_id,
                    "event_ids": list(claim.event_ids),
                    "raw_rows": rows,
                    "events": events,
                },
                default=_json_value,
            )
        )
        validate_input(document)
        encoded = canonical_payload(document)
        if len(encoded.encode()) > MAX_INPUT_BYTES:
            raise LakeError("lake_export_input_byte_limit")
        control.execute(
            "INSERT INTO lake_export_batches (batch_id,catalog_id,input_json,input_sha256) VALUES (?,?,?,?)",
            [claim.batch_id, self.catalog_id, encoded, payload_sha256(encoded)],
        )
        return document

    def _input(self, now):
        with self._control() as control:
            document = self._pending(control)
            if document:
                return document
            claim = claim_outbox_batch(
                control, owner=self.owner, limit=self.batch_size, now=now
            )
            if claim is None:
                return None
            control.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
            try:
                document = self._freeze(control, claim)
                control.execute("COMMIT")
                return document
            except BaseException:
                control.execute("ROLLBACK")
                raise

    def _publish(self, document):
        # No DSN is serialized; only the env-variable name goes into the request.
        with tempfile.TemporaryDirectory(
            prefix="export-", dir=self.spec.data_root
        ) as directory:
            root = Path(directory)
            reply = root / "reply.json"
            request = root / "request.json"
            request.write_text(
                json.dumps(
                    {
                        "spec": {
                            **asdict(self.spec),
                            "data_root": str(self.spec.data_root),
                            "extension_dir": str(self.spec.extension_dir),
                        },
                        "document": document,
                        "reply": str(reply),
                    }
                )
            )
            request.chmod(0o600)
            env = {
                **os.environ,
                self.spec.catalog_dsn_env: make_conninfo(
                    self.spec.dsn(), application_name=APPLICATION_PREFIX + self.token
                ),
            }
            started = time.monotonic()
            with (root / "error.log").open("wb") as errors:
                child = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "drover.server.lake.export_worker",
                        str(request),
                    ],
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=errors,
                    start_new_session=True,
                )
                try:
                    while child.poll() is None:
                        self._check()
                        if time.monotonic() - started > 30:
                            raise LakeError("lake_export_deadline")
                        try:
                            if _rss(child.pid) > 2 * 1024**3:
                                raise LakeError("lake_export_rss_limit")
                        except LakeError as exc:
                            if exc.code != "analytics_rss_monitor_unavailable":
                                raise
                            try:
                                child.wait(timeout=0.05)
                            except subprocess.TimeoutExpired:
                                raise exc
                        time.sleep(0.02)
                    self._check()
                    if child.returncode or not reply.is_file():
                        raise LakeError("lake_export_process_failed")
                    result = json.loads(reply.read_text())
                    if result.get("error"):
                        raise LakeError(result["error"])
                    return result
                finally:
                    if child.poll() is None:
                        try:
                            os.killpg(child.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        except PermissionError:
                            child.wait(timeout=0.05)
                    child.wait()

    def _acknowledge(self, document, receipt, now):
        self._check()
        if (
            receipt["input_sha256"] != payload_sha256(canonical_payload(document))
            or receipt["batch_id"] != document["batch_id"]
            or receipt["catalog_id"] != self.catalog_id
        ):
            raise LakeError("lake_export_receipt_mismatch")
        with self._control() as control:
            control.execute("BEGIN")
            try:
                frozen = control.execute(
                    "SELECT input_sha256,receipt_sha256 FROM lake_export_batches WHERE batch_id=? FOR UPDATE",
                    [document["batch_id"]],
                ).fetchone()
                if (
                    frozen is None
                    or frozen[0] != receipt["input_sha256"]
                    or frozen[1] not in (None, receipt_hash(receipt))
                ):
                    raise LakeError("lake_export_acknowledgement_mismatch")
                control.execute(
                    "UPDATE lake_export_batches SET receipt_sha256=?,acknowledged_at=? WHERE batch_id=?",
                    [receipt_hash(receipt), now, document["batch_id"]],
                )
                updated = control.execute(
                    "UPDATE control_outbox_batches SET state='acknowledged',published_at=?,acknowledged_at=? WHERE batch_id=? AND state='claimed' RETURNING batch_id",
                    [now, now, document["batch_id"]],
                ).fetchone()
                if not updated:
                    raise LakeError("lake_export_acknowledgement_mismatch")
                members = control.execute(
                    "UPDATE control_outbox_events SET state='acknowledged',published_at=?,acknowledged_at=? WHERE batch_id=? AND state='claimed' RETURNING event_id",
                    [now, now, document["batch_id"]],
                ).fetchall()
                if sorted(r[0] for r in members) != sorted(document["event_ids"]):
                    raise LakeError("lake_export_membership_mismatch")
                self._check()
                control.execute("COMMIT")
            except BaseException:
                control.execute("ROLLBACK")
                raise

    def run_once(self, *, now=None):
        with self._batch_lock:
            return self._run_once(now=now)

    def _run_once(self, *, now=None):
        self._check()
        stamp = now or datetime.now(timezone.utc)
        document = self._input(stamp)
        if document is None:
            return {"exported": 0, "acknowledged": 0, "replayed": False}
        result = self._publish(document)
        self._acknowledge(document, result["receipt"], datetime.now(timezone.utc))
        return {
            "exported": result["receipt"]["canonical_rows"],
            "acknowledged": result["receipt"]["raw_rows"],
            "replayed": result["replayed"],
        }
