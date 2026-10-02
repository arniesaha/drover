"""One admitted disposable OS process per analytical query, with hard limits."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .runtime import LakeError, LakeSpec, lake_connection


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
            child = subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                stdout=output,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
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
                    raise LakeError(result["error"])
                if len(result.get("rows", [])) > limits.rows:
                    raise LakeError("analytics_row_limit_exceeded")
                return {
                    **result,
                    "peak_rss_bytes": peak,
                    "elapsed_seconds": time.monotonic() - started,
                }
            finally:
                if child.poll() is None:
                    import signal

                    try:
                        os.killpg(child.pid, signal.SIGKILL)
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
) -> dict:
    started_at = time.monotonic()
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
                + ".lock"
            ),
            limits=limits,
            cwd=root,
            started_at=started_at,
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
    with reader_fence(spec.dsn()), lake_connection(spec) as con:
        import re

        if re.search(r"\bAT\s*\(\s*(VERSION|TIMESTAMP)\b", request["sql"], re.I):
            raise LakeError("analytics_time_travel_disabled")
        statements = con.extract_statements(request["sql"])
        if len(statements) != 1 or str(statements[0].type) != "StatementType.SELECT":
            raise LakeError("analytics_read_query_required")
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
        return {"columns": [c[0] for c in cursor.description], "rows": rows}


if __name__ == "__main__":
    try:
        reply = _worker(json.loads(Path(sys.argv[1]).read_text()))
    except LakeError as exc:
        reply = {"error": exc.code}
    except Exception:
        reply = {"error": "analytics_unavailable"}
    print(json.dumps(reply, default=str))
