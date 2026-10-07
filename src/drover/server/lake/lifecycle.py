"""One explicit exporter lifecycle; startup never provisions or retries legacy."""

import logging
import os
import threading

from .export_guard import GUARD_SCHEMA
from .exporter import LakeOutboxExporter
from .runtime import LakeError, sanitize_detail
from .serving import HistoryConnection, lake_spec, selected_config
from .serving_proof import catalog_identity

log = logging.getLogger(__name__)

# Exports between serving-proof checkpoints: ~45 min at prod's peak rate of
# ~11/min, so a serving child re-checks at most a few hundred exports.
CHECKPOINT_EVERY_EXPORTS = 500

# SQLSTATEs for a missing schema (3F000) or function (42883).
_MISSING_GUARD_SQLSTATES = {"3F000", "42883"}


def _guard_missing(exc: BaseException) -> bool:
    """True when the catalog lacks the drover_lake_export schema or activate()."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if getattr(
            exc, "sqlstate", None
        ) in _MISSING_GUARD_SQLSTATES and GUARD_SCHEMA in str(exc):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def _cause(exc: BaseException, config) -> str:
    """Sanitized exception class and first line; never a DSN or password."""
    secrets = [
        os.environ.get(value)
        for name in ("exporter_dsn_env", "reader_dsn_env", "catalog_dsn_env")
        if isinstance(value := getattr(config.analytics, name, None), str)
    ]
    return sanitize_detail(exc, *secrets)


class ExporterLifecycle:
    def __init__(self, config):
        self.config = config
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread = None
        self._error = None
        self._running = False
        self._exports_since_checkpoint = 0

    def start(self, *, shutdown_event):
        if self._thread is not None:
            raise LakeError("lake_exporter_already_started")
        self._thread = threading.Thread(
            target=self._run,
            args=(shutdown_event,),
            name="ducklake-exporter",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(10):
            self._stop.set()
            raise LakeError("lake_exporter_start_deadline")
        if self._error:
            self.stop()
            raise LakeError(self._error)

    def _run(self, shutdown):
        try:
            # Create, use and release the dedicated fence on this owner thread.
            with LakeOutboxExporter(
                control_path=self.config.duckdb_path,
                spec=lake_spec(self.config.analytics, exporter=True),
            ) as exporter:
                # Both explicitly configured credentials must address the same
                # catalog. Do not rely on ambient path registration for startup.
                _check_export_catalog(self.config, exporter)
                if self.config.analytics.retire_legacy_writers:
                    from .writer_gate import activate_retirement

                    if (
                        selected_config(self.config.duckdb_path)
                        != self.config.analytics
                    ):
                        raise LakeError("lake_retirement_config_mismatch")
                    activate_retirement(self.config.duckdb_path)
                from .task_projection import refresh_if_provisioned

                refresh_if_provisioned(
                    self.config.duckdb_path, lake_fence=getattr(exporter, "fence", None)
                )
                self._running = True
                self._ready.set()
                # A restart after a long run must not wait 500 exports to bound
                # the serving check again.
                self._checkpoint(exporter)
                while not self._stop.is_set() and not shutdown.is_set():
                    if self.config.analytics.retire_legacy_writers:
                        from .writer_gate import legacy_derived_write

                        with legacy_derived_write(self.config.duckdb_path) as allowed:
                            if (
                                selected_config(self.config.duckdb_path)
                                != self.config.analytics
                            ):
                                raise LakeError("lake_retirement_config_mismatch")
                            if allowed:
                                raise LakeError("lake_retirement_lost")
                            result = exporter.run_once()
                            if result and (
                                result.get("exported") or result.get("acknowledged")
                            ):
                                refresh_if_provisioned(
                                    self.config.duckdb_path,
                                    lake_fence=getattr(exporter, "fence", None),
                                )
                    else:
                        result = exporter.run_once()
                        if result and (
                            result.get("exported") or result.get("acknowledged")
                        ):
                            refresh_if_provisioned(
                                self.config.duckdb_path,
                                lake_fence=getattr(exporter, "fence", None),
                            )
                    if result and result.get("acknowledged"):
                        self._exports_since_checkpoint += 1
                        if self._exports_since_checkpoint >= CHECKPOINT_EVERY_EXPORTS:
                            self._checkpoint(exporter)
                    self._stop.wait(0.25)
        except Exception as exc:
            if isinstance(exc, LakeError):
                self._error = exc.code
            elif _guard_missing(exc):
                self._error = "lake_export_not_provisioned"
            else:
                self._error = "lake_export_unavailable"
            log.warning(
                "DuckLake exporter stopped: %s (%s)",
                self._error,
                _cause(exc, self.config),
            )
        finally:
            self._running = False
            self._ready.set()

    def _checkpoint(self, exporter):
        """Advance the serving checkpoint; a failure leaves serving to the
        full check and never stops exports."""
        from .serving_proof import advance_checkpoint

        self._exports_since_checkpoint = 0
        try:
            record = advance_checkpoint(
                lake_spec(self.config.analytics),
                self.config.analytics.verification_sha256,
                fence=exporter.fence,
            )
        except Exception as exc:  # noqa: BLE001 - exports must keep running
            log.warning(
                "serving proof checkpoint not advanced: %s (%s)",
                exc.code if isinstance(exc, LakeError) else "unexpected",
                _cause(exc, self.config),
            )
            return
        if record is not None:
            log.info(
                "serving proof checkpoint at snapshot %s (%s exports since proof)",
                record["snapshot"],
                record["exports"],
            )

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(35)
            if self._thread.is_alive():
                raise LakeError("lake_exporter_stop_deadline")

    def health(self):
        return {"enabled": self._running, "last_error": self._error}


def selected_exporter(config):
    if config.analytics.backend == "legacy":
        from drover.server.control_exporter import ControlOutboxExporter

        return ControlOutboxExporter(
            control_path=config.duckdb_path,
            analytical_path=config.duckdb_path,
            parquet_dir=config.parquet_dir,
            acknowledgement_retention_days=config.control_store.outbox_retention_days,
        )
    # Restart-selected DuckLake is a complete read/write selection. A separate
    # opt-in bit used to leave a hub serving the lake while silently not
    # exporting new control events; do not permit that split brain.
    return ExporterLifecycle(config)


def _check_export_catalog(config, exporter):
    reader = lake_spec(config.analytics)
    if catalog_identity(reader) != catalog_identity(exporter.spec):
        raise LakeError("lake_export_catalog_mismatch")
    with HistoryConnection(config.analytics) as history:
        history.execute("SELECT 1")


def export_ingested_events(config, dedup_keys, *, timeout_seconds=30, max_passes=100):
    """Synchronously publish a CLI file, bounded by passes and elapsed time.

    Check canonical dedup keys rather than incoming IDs: a retry may name an
    already committed event differently. Never report success for rejected or
    still outstanding rows. Each pass retains the normal byte/count limits.
    """
    import time
    from contextlib import nullcontext

    from drover.server.db import control_plane_connection

    keys = sorted(set(dedup_keys))
    if not keys:
        return
    if config.analytics.backend == "legacy":
        owner = nullcontext(selected_exporter(config))
    else:
        owner = LakeOutboxExporter(
            control_path=config.duckdb_path,
            spec=lake_spec(config.analytics, exporter=True),
        )
    deadline = time.monotonic() + timeout_seconds
    with owner as exporter:
        if config.analytics.backend == "ducklake":
            _check_export_catalog(config, exporter)
        for _ in range(max_passes):
            # CLI publication bypasses only the legacy flush delay, retaining
            # its byte/count limits and the hub's default 5s background age.
            if config.analytics.backend == "legacy":
                exporter.run_once(force_flush=True)
            else:
                exporter.run_once()
            outstanding = 0
            with control_plane_connection(config.duckdb_path) as control:
                for offset in range(0, len(keys), 1000):
                    chunk = keys[offset : offset + 1000]
                    placeholders = ",".join("?" for _ in chunk)
                    rows = control.execute(
                        f"""SELECT o.state, o.rejection_reason
                        FROM harness_events e JOIN control_outbox_events o
                        ON o.event_id=e.event_id
                        WHERE e.dedup_key IN ({placeholders})""",
                        chunk,
                    ).fetchall()
                    outstanding += len(chunk) - len(rows)
                    for state, reason in rows:
                        if state == "rejected":
                            raise RuntimeError(f"incoming event rejected: {reason}")
                        outstanding += state != "acknowledged"
            if not outstanding:
                if config.analytics.backend == "ducklake":
                    from .task_projection import refresh_if_provisioned

                    refresh_if_provisioned(
                        config.duckdb_path, lake_fence=exporter.fence
                    )
                return
            if time.monotonic() >= deadline:
                break
    raise RuntimeError("incoming export deadline: events remain unacknowledged")
