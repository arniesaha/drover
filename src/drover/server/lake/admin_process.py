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


def run_admin(request: dict, root: Path) -> tuple[dict, int]:
    job = root / "admin-request.json"
    reply = root / "admin-reply.json"
    job.write_text(json.dumps({**request, "reply": str(reply)}))
    job.chmod(0o600)
    reply.unlink(missing_ok=True)
    peak = 0
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
                    sample = _rss(os.getpid()) + _rss(child.pid)
                    peak = max(peak, sample)
                except LakeError as exc:
                    # Darwin can stop exposing task info just before waitpid
                    # reports exit. Only tolerate a child that actually exits.
                    try:
                        child.wait(timeout=0.05)
                    except subprocess.TimeoutExpired:
                        raise exc
                if peak > RSS_CEILING:
                    raise LakeError("rebuild_rss_limit_exceeded")
                if time.monotonic() - started > 1800:
                    raise LakeError("rebuild_partition_deadline_exceeded")
                time.sleep(0.02)
            if child.returncode or not reply.is_file():
                raise LakeError("rebuild_partition_process_failed")
            result = json.loads(reply.read_text())
            if result.get("error"):
                raise LakeError(result["error"])
            return result, peak
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
