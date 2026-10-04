"""One explicit exporter lifecycle; startup never provisions or retries legacy."""

import logging
import threading

from .exporter import LakeOutboxExporter
from .runtime import LakeError
from .serving import HistoryConnection, lake_spec, selected_config
from .serving_proof import catalog_identity

log = logging.getLogger(__name__)


class ExporterLifecycle:
    def __init__(self, config):
        self.config = config
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread = None
        self._error = None
        self._running = False

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
                reader = lake_spec(self.config.analytics)
                if catalog_identity(reader) != catalog_identity(exporter.spec):
                    raise LakeError("lake_export_catalog_mismatch")
                with HistoryConnection(self.config.analytics) as history:
                    history.execute("SELECT 1")
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
                    self._stop.wait(0.25)
        except Exception as exc:
            self._error = (
                exc.code if isinstance(exc, LakeError) else "lake_export_unavailable"
            )
            log.warning("DuckLake exporter stopped: %s", self._error)
        finally:
            self._running = False
            self._ready.set()

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
        )
    if not config.analytics.exporter_enabled:
        return None
    return ExporterLifecycle(config)
