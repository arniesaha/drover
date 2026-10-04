"""Disposable offline jobs with a sampled aggregate RSS ceiling and data-root spill."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .query_process import _rss
from .runtime import LakeError

RSS_CEILING = 5 * 1024**3 // 2


def run_admin(
    request: dict, root: Path, *, rss_ceiling: int = RSS_CEILING
) -> tuple[dict, int]:
    """Run one disposable worker under a caller-selected aggregate RSS cap.

    Only the offline rebuild command supplies a non-default cap.  Keeping the
    override at this process boundary prevents it from changing the server or
    standalone verification safety budgets.
    """
    job = root / "admin-request.json"
    reply = root / "admin-reply.json"
    job.write_text(json.dumps({**request, "reply": str(reply)}))
    job.chmod(0o600)
    reply.unlink(missing_ok=True)
    peak = 0
    coordinator_rss = 0
    child_rss = 0
    failure = None
    started = time.monotonic()
    with (root / "admin-error.log").open("wb") as errors:
        child = subprocess.Popen(
            [sys.executable, "-m", "drover.server.lake.rebuild_worker", str(job)],
            stdout=subprocess.DEVNULL,
            stderr=errors,
            start_new_session=True,
        )
        try:
            while child.poll() is None:
                try:
                    coordinator_rss = _rss(os.getpid())
                    child_rss = _rss(child.pid)
                    sample = coordinator_rss + child_rss
                    peak = max(peak, sample)
                except LakeError as exc:
                    # Darwin can stop exposing task info just before waitpid
                    # reports exit. Only tolerate a child that actually exits.
                    try:
                        child.wait(timeout=0.05)
                    except subprocess.TimeoutExpired:
                        raise exc
                if peak > rss_ceiling:
                    failure = "supervisor_aggregate_rss_limit"
                    raise LakeError("rebuild_rss_limit_exceeded")
                if time.monotonic() - started > 1800:
                    failure = "supervisor_partition_deadline"
                    raise LakeError("rebuild_partition_deadline_exceeded")
                time.sleep(0.02)
            if child.returncode or not reply.is_file():
                failure = "child_process"
                raise LakeError("rebuild_partition_process_failed")
            result = json.loads(reply.read_text())
            if result.get("error"):
                failure = "child_worker"
                raise LakeError(
                    result["error"],
                    (result.get("detail") or None) and str(result["detail"])[:300],
                )
            return result, peak
        except LakeError as exc:
            # A failed rebuild retains its data root for inspection.  Record
            # the cap and the last split sample there rather than relying on
            # stderr, which is deliberately scoped to the disposable child.
            (root / "admin-supervision-failure.json").write_text(
                json.dumps(
                    {
                        "operation": request.get("operation"),
                        "error": exc.code,
                        "detail": exc.detail,
                        "failure_attribution": failure or "supervisor",
                        "rss_ceiling_bytes": rss_ceiling,
                        "peak_rss_bytes": peak,
                        "last_rss_sample_bytes": {
                            "coordinator": coordinator_rss,
                            "child": child_rss,
                        },
                    },
                    indent=2,
                )
            )
            raise
        finally:
            if child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    # The same kernel exit transition can return EPERM here.
                    # An actually running child still causes an explicit error.
                    child.wait(timeout=0.05)
            child.wait()
            job.unlink(missing_ok=True)
            reply.unlink(missing_ok=True)
