"""One admitted disposable OS process per analytical query, with hard limits."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .runtime import QUERY_CHILD_SETTINGS, LakeError, LakeSpec, lake_connection


@dataclass(frozen=True)
class QueryLimits:
    deadline_seconds: float = 5.0
    rss_bytes: int = 2 * 1024**3
    rows: int = 1000
    bytes: int = 1024 * 1024

    def __post_init__(self):
        if (
            not math.isfinite(self.deadline_seconds)
            or not 0 < self.deadline_seconds <= 5
            or not 0 < self.rss_bytes <= 2 * 1024**3
            or not 0 < self.rows <= 10000
            or not 0 < self.bytes <= 4 * 1024**2
        ):
            raise ValueError("query limits may only tighten the release ceilings")


def _rss(pid: int) -> int:
    from drover.server.process_memory import process_rss

    try:
        return process_rss(pid)
    except (OSError, ValueError, subprocess.SubprocessError):
        raise LakeError("analytics_rss_monitor_unavailable") from None


class _SpawnedQueryProcess:
    """The small ``Popen`` surface the disposable-reader supervisor needs.

    ``subprocess.Popen`` is permitted to take its fork/exec path on macOS even
    when its arguments look suitable for ``posix_spawn``.  Forking a large,
    long-lived hub transiently charges copy-on-write allocator pages to the
    hub's physical-footprint high-water mark.  Use ``posix_spawn`` directly so
    a reader launch can never copy the hub address space.
    """

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None

    @staticmethod
    def _returncode(status: int) -> int:
        if os.WIFEXITED(status):
            return os.WEXITSTATUS(status)
        if os.WIFSIGNALED(status):
            return -os.WTERMSIG(status)
        return status

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        try:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            return self.returncode
        if pid:
            self.returncode = self._returncode(status)
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.returncode is None:
            self.poll()
            if self.returncode is not None:
                break
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(["disposable-query"], timeout)
            time.sleep(0.005)
        return self.returncode

    def kill(self) -> None:
        os.kill(self.pid, signal.SIGKILL)


def _spawn_disposable_query(
    command: list[str], *, environment: dict[str, str] | None, output
) -> _SpawnedQueryProcess:
    """Exec a query child without ever forking the hub process.

    The worker receives only absolute paths and does not need a changed
    working directory.  New descriptors are non-inheritable (PEP 446); stdout
    is the bounded temporary file and stderr is deliberately discarded.
    """

    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        actions = [
            (os.POSIX_SPAWN_DUP2, output.fileno(), 1),
            (os.POSIX_SPAWN_DUP2, devnull, 2),
        ]
        env = os.environ if environment is None else environment
        spawn = os.posix_spawn if os.path.sep in command[0] else os.posix_spawnp
        return _SpawnedQueryProcess(
            spawn(command[0], command, env, file_actions=actions)
        )
    finally:
        os.close(devnull)


def run_disposable(
    command: list[str],
    *,
    admission_path: Path,
    limits: QueryLimits,
    cwd: Path,
    env: dict[str, str] | None = None,
    started_at: float | None = None,
) -> dict:
    """Supervise a child writing a bounded JSON reply to stdout.

    The admission lock is shared across processes, not a per-client semaphore.
    Waiting for admission consumes the same end-to-end five-second deadline.
    """
    started = time.monotonic() if started_at is None else started_at
    deadline = started + limits.deadline_seconds
    peak = 0
    admission_path.parent.mkdir(parents=True, exist_ok=True)
    with admission_path.open("a+b") as admission:
        while True:
            if time.monotonic() >= deadline:
                raise LakeError("analytics_admission_deadline")
            try:
                fcntl.flock(admission, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LakeError("analytics_admission_deadline")
                time.sleep(0.01)
        # File-backed stdout avoids a pipe deadlock. Growth is checked each
        # sample; successful results are never read beyond the hard byte cap.
        with tempfile.TemporaryFile() as output:
            # ``cwd`` is intentionally not passed through: each caller gives
            # the child absolute request/scratch paths.  Changing it would
            # force subprocess' fork path, defeating the hub-footprint guard.
            child = _spawn_disposable_query(command, environment=env, output=output)
            from drover.server.process_memory import memory_guard

            guard = memory_guard()
            guard.child_started(child.pid)
            try:
                while child.poll() is None:
                    if time.monotonic() >= deadline:
                        raise LakeError("analytics_deadline_exceeded")
                    if os.fstat(output.fileno()).st_size > limits.bytes:
                        raise LakeError("analytics_byte_limit_exceeded")
                    try:
                        sample = _rss(child.pid)
                        guard.child_sample(sample)
                        peak = max(peak, sample)
                    except LakeError as exc:
                        try:
                            child.wait(timeout=0.05)
                        except subprocess.TimeoutExpired:
                            raise exc
                    if peak > limits.rss_bytes:
                        raise LakeError("analytics_rss_limit_exceeded")
                    time.sleep(0.01)
                if time.monotonic() >= deadline:
                    raise LakeError("analytics_deadline_exceeded")
                if child.returncode:
                    raise LakeError("analytics_process_failed")
                output.seek(0)
                payload = output.read(limits.bytes + 1)
                if len(payload) > limits.bytes:
                    raise LakeError("analytics_byte_limit_exceeded")
                try:
                    result = json.loads(payload)
                    if not isinstance(result, dict):
                        raise ValueError()
                except (ValueError, UnicodeDecodeError):
                    raise LakeError("analytics_invalid_process_reply") from None
                if result.get("error"):
                    from .runtime import DETAIL_LIMIT

                    detail = result.get("detail")
                    raise LakeError(
                        str(result["error"]),
                        str(detail)[:DETAIL_LIMIT] if detail else None,
                    )
                if len(result.get("rows", [])) > limits.rows:
                    raise LakeError("analytics_row_limit_exceeded")
                return {
                    **result,
                    "peak_rss_bytes": peak,
                    "elapsed_seconds": time.monotonic() - started,
                }
            finally:
                if child.poll() is None:
                    try:
                        child.kill()
                    except ProcessLookupError:
                        pass
                    except PermissionError:
                        child.wait(timeout=0.05)
                child.wait()
                guard.child_finished(child.pid)


def query(
    spec: LakeSpec,
    sql: str,
    params: list | None = None,
    *,
    limits: QueryLimits = QueryLimits(),
    serving: dict | None = None,
) -> dict:
    started_at = time.monotonic()
    # Exporter prerequisites must not queue behind cockpit reads. Both lanes
    # still admit only one disposable child and retain the same hard limits.
    maintenance = bool(
        serving
        and (
            serving.get("exporter_check")
            or serving.get("checkpoint")
            or serving.get("task_projection")
        )
    )
    with tempfile.TemporaryDirectory(prefix="drover-lake-query-") as scratch:
        root = Path(scratch)
        request = root / "request.json"
        request.write_text(
            json.dumps(
                {
                    "spec": {
                        **asdict(spec),
                        "data_root": str(spec.data_root),
                        "extension_dir": str(spec.extension_dir),
                    },
                    "sql": sql,
                    "params": params or [],
                    "limits": asdict(limits),
                    "serving": serving,
                    "spill_directory": str(root / "spill"),
                }
            )
        )
        if request.stat().st_size > 262_144:
            raise LakeError("analytics_request_byte_limit_exceeded")
        request.chmod(0o600)
        return run_disposable(
            [sys.executable, "-m", __name__, str(request)],
            admission_path=Path(tempfile.gettempdir())
            / f"drover-lake-{os.getuid()}"
            / (
                hashlib.sha256(str(spec.data_root.resolve()).encode()).hexdigest()
                + (".maintenance.lock" if maintenance else ".lock")
            ),
            limits=limits,
            cwd=root,
            started_at=started_at,
        )


def load_identities(con, identities: list) -> None:
    """Fill ``memory_session_identity`` in one statement.

    ``executemany`` costs ~0.2 ms a row, which every serving child paid (0.6 s
    at 3,000 sessions); one JSON parameter costs ~3 ms for the same rows.
    """
    con.execute(
        "INSERT INTO memory_session_identity SELECT r[1], r[2], r[3],"
        " r[4]::TIMESTAMPTZ FROM (SELECT unnest(CAST(?::JSON AS VARCHAR[][])) AS r)",
        [json.dumps(identities)],
    )


def _worker(request: dict) -> dict:
    from .fence import reader_fence

    spec = LakeSpec(
        **{
            **request["spec"],
            "data_root": Path(request["spec"]["data_root"]),
            "extension_dir": Path(request["spec"]["extension_dir"]),
        }
    )
    limits = QueryLimits(**request["limits"])
    settings = dict(QUERY_CHILD_SETTINGS)
    if request.get("spill_directory"):
        # Inside the query's private scratch directory: removed with it.
        settings["temp_directory"] = request["spill_directory"]
    with reader_fence(spec.dsn()), lake_connection(spec, settings=settings) as con:
        import re

        if re.search(r"\bAT\s*\(\s*(VERSION|TIMESTAMP)\b", request["sql"], re.I):
            raise LakeError("analytics_time_travel_disabled")
        statements = con.extract_statements(request["sql"])
        if len(statements) != 1 or str(statements[0].type) != "StatementType.SELECT":
            raise LakeError("analytics_read_query_required")
        if serving := request.get("serving"):
            if serving.get("operation"):
                # A read model binds the snapshot it reads: taken before the
                # transaction, rechecked inside it (read_models.run_model).
                serving["snapshot_before"] = con.execute(
                    "SELECT max(snapshot_id) FROM lake.snapshots()"
                ).fetchone()[0]
            con.execute("BEGIN TRANSACTION")
            from .serving_proof import check_proof

            checked = check_proof(spec, con, serving["verification_sha256"])
            if serving.get("checkpoint"):
                return {"checkpoint": checked, "rows": []}
            con.execute(
                "CREATE TEMP TABLE memory_session_identity(harness_session_id VARCHAR, native_session_id VARCHAR, summary_session_id VARCHAR, started_at TIMESTAMPTZ)"
            )
            identities = serving.get("identities", [])
            if len(identities) > 10000:
                raise LakeError("analytics_identity_limit_exceeded")
            if identities:
                load_identities(con, identities)
            con.execute("""CREATE TEMP VIEW agent_events AS
                SELECT * EXCLUDE(timestamp,repo_owner,repo_name), TRY_CAST(timestamp AS TIMESTAMPTZ) AS timestamp,
                  COALESCE(repo_owner, CASE WHEN json_valid(raw_data) THEN json_extract_string(raw_data,'$._repo_owner') END) AS repo_owner,
                  COALESCE(repo_name, CASE WHEN json_valid(raw_data) THEN json_extract_string(raw_data,'$._repo_name') END) AS repo_name,
                  CASE WHEN dedup_key_source='outbox' OR
                    (CASE WHEN json_valid(raw_data) THEN json_extract_string(raw_data,'$.source') END)='control'
                    THEN 'control' ELSE 'native' END AS source
                FROM lake.agent_events e WHERE dedup_key_source='outbox' OR
                    (CASE WHEN json_valid(raw_data) THEN json_extract_string(raw_data,'$.source') END)='control' OR NOT EXISTS (
                  SELECT 1 FROM memory_session_identity m WHERE e.session_id IN (m.harness_session_id,m.native_session_id)
                    AND m.native_session_id IS DISTINCT FROM m.harness_session_id)""")
            con.execute(
                "CREATE TEMP VIEW control_memory_events AS SELECT * FROM agent_events WHERE source='control'"
            )
        if (request.get("serving") or {}).get("coverage_build"):
            from .coverage import build_in_child

            return build_in_child(con)
        if (request.get("serving") or {}).get("task_projection"):
            from .task_projection import build_in_child

            return build_in_child(
                con,
                build=request["serving"]["task_projection"] == "build",
                kind=request["serving"].get("projection_kind"),
                after=request["serving"].get("projection_after"),
            )
        if (request.get("serving") or {}).get("operation"):
            from .read_models import run_model

            return run_model(con, request["serving"], limits)
        cursor = con.execute(request["sql"], request["params"])
        rows = []
        size = 0
        while row := cursor.fetchone():
            rows.append(row)
            size += len(json.dumps(row, default=str).encode())
            if len(rows) > limits.rows:
                raise LakeError("analytics_row_limit_exceeded")
            if size > limits.bytes:
                raise LakeError("analytics_byte_limit_exceeded")
        return {
            "columns": [c[0] for c in cursor.description],
            "types": [str(c[1]) for c in cursor.description],
            "rows": rows,
        }


def error_reply(exc: BaseException, *secrets: str | None) -> dict:
    """The child's reply for a failed query: a stable code plus its cause.

    ``detail`` is the exception class and first message line, scrubbed of DSN
    material, so the hub log says *why* (``OutOfMemoryException: ...``)
    instead of only ``analytics_unavailable``.
    """
    import duckdb

    from drover.server.cockpit.analytics import AnalyticsSnapshotChangedError

    from .runtime import sanitize_detail

    if isinstance(exc, LakeError):
        # A LakeError's detail is sanitized where it is raised.
        return {"error": exc.code, **({"detail": exc.detail} if exc.detail else {})}
    if isinstance(exc, AnalyticsSnapshotChangedError):
        code = "snapshot_changed"
    elif isinstance(exc, duckdb.OutOfMemoryException):
        code = "analytics_memory_limit_exceeded"
    else:
        code = "analytics_unavailable"
    return {"error": code, "detail": sanitize_detail(exc, *secrets)}


if __name__ == "__main__":
    request: dict = {}
    try:
        request = json.loads(Path(sys.argv[1]).read_text())
        reply = _worker(request)
    except Exception as exc:
        dsn = os.environ.get((request.get("spec") or {}).get("catalog_dsn_env") or "")
        reply = error_reply(exc, dsn)
    print(json.dumps(reply, default=str))
