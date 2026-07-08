"""gRPC OTLP TraceService receiver.

Wraps an in-process gRPC server that delegates to ingest_otlp_request.
The Export handler always returns OK — even on ingest failure — so OTel
exporters don't enter a retry storm. Ingest errors are logged.
"""

from __future__ import annotations

import logging
import threading
from concurrent import futures
from pathlib import Path

import grpc
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2 as ts
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2_grpc as tsg

from drover.server.otlp.ingest import ingest_otlp_request

log = logging.getLogger("drover.otlp.receiver")


class _Servicer(tsg.TraceServiceServicer):
    def __init__(
        self,
        *,
        parquet_dir: Path,
        duckdb_path: Path,
        ingest_lock: threading.Lock,
        span_job_stream: object | None = None,
    ) -> None:
        self.parquet_dir = parquet_dir
        self.duckdb_path = duckdb_path
        self._lock = ingest_lock
        self._span_job_stream = span_job_stream

    def Export(self, request, context):  # noqa: N802 — gRPC method name
        try:
            with self._lock:
                stats = ingest_otlp_request(
                    request,
                    parquet_dir=self.parquet_dir,
                    duckdb_path=self.duckdb_path,
                    span_job_stream=self._span_job_stream,
                )
            log.debug(
                "OTLP Export: read=%d inserted=%d dupes=%d errors=%d",
                stats.read,
                stats.inserted,
                stats.skipped_dupes,
                stats.errors,
            )
        except Exception:  # noqa: BLE001 — never crash the server thread
            log.exception("OTLP ingest failed; returning OK to client")
        return ts.ExportTraceServiceResponse()


class OTLPReceiver:
    """Lifecycle wrapper around the gRPC server."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        parquet_dir: Path,
        duckdb_path: Path,
        max_workers: int = 4,
        span_job_stream: object | None = None,
    ) -> None:
        self.host = host
        self._configured_port = port
        self._actual_port: int | None = None
        self.parquet_dir = Path(parquet_dir)
        self.duckdb_path = Path(duckdb_path)
        self.max_workers = max_workers
        self._span_job_stream = span_job_stream
        self._server: grpc.Server | None = None
        self._lock = threading.Lock()  # serialize DuckDB single-writer access

    @property
    def port(self) -> int:
        """The actual bound port (resolved after start)."""
        return (
            self._actual_port
            if self._actual_port is not None
            else self._configured_port
        )

    def start(self) -> None:
        if self._server is not None:
            return
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=self.max_workers))
        servicer = _Servicer(
            parquet_dir=self.parquet_dir,
            duckdb_path=self.duckdb_path,
            ingest_lock=self._lock,
            span_job_stream=self._span_job_stream,
        )
        tsg.add_TraceServiceServicer_to_server(servicer, server)
        bound = server.add_insecure_port(f"{self.host}:{self._configured_port}")
        if bound == 0:
            raise RuntimeError(f"failed to bind {self.host}:{self._configured_port}")
        self._actual_port = bound
        server.start()
        self._server = server
        log.info("OTLP receiver listening on %s:%d", self.host, bound)

    def stop(self, grace: float = 5.0) -> None:
        srv = self._server
        if srv is None:
            return
        self._server = None
        try:
            srv.stop(grace).wait(timeout=grace + 1.0)
        except Exception:  # noqa: BLE001
            log.exception("error during gRPC server shutdown")
        log.info("OTLP receiver stopped")
