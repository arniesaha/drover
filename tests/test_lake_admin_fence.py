"""Live supervisor fence enforcement; no production engines or credentials."""

import json
import os
import socket
import subprocess
import sys

import pytest

from drover.server.lake import admin_process
from drover.server.lake.runtime import LakeError


@pytest.mark.parametrize(
    "job_request",
    [{"operation": "publish"}, {"operation": "deprecate_legacy", "apply": True}],
)
def test_missing_fence_refuses_before_spawn(tmp_path, monkeypatch, job_request):
    def forbidden(*args, **kwargs):
        pytest.fail("spawned without fence")

    monkeypatch.setattr(admin_process.subprocess, "Popen", forbidden)
    with pytest.raises(LakeError, match="lake_admin_fence_required"):
        admin_process.run_admin(job_request, tmp_path)
    assert not (tmp_path / "admin-request.json").exists()


@pytest.mark.parametrize(
    "phase", ["before", "running", "exited", "success", "readonly"]
)
def test_real_child_fence_lifecycle(tmp_path, monkeypatch, phase):
    # Socket handshake removes timing guesses: the child exists before the
    # running-loss check, and has exited before the post-exit-loss check.
    parent, peer = socket.socketpair()
    parent.settimeout(3)
    original = subprocess.Popen
    children = []
    checks = []
    code = """
import json, socket, sys, time
from pathlib import Path
sock=socket.socket(fileno=int(sys.argv[1]))
job=json.loads(Path(sys.argv[2]).read_text())
sock.sendall(b'R')
if sys.argv[3]=='running':
    sock.recv(1)
else:
    Path(job['reply']).write_text(json.dumps({'verified': True}))
"""

    def spawn(args, **kwargs):
        child = original(
            [sys.executable, "-c", code, str(peer.fileno()), args[-1], phase],
            pass_fds=(peer.fileno(),),
            **kwargs,
        )
        children.append(child)
        assert parent.recv(1) == b"R"
        if phase != "running":
            child.wait(timeout=3)
        return child

    def check():
        checks.append(True)
        if phase == "before" or (phase in {"running", "exited"} and len(checks) > 1):
            raise LakeError("lake_fence_lost")

    monkeypatch.setattr(admin_process.subprocess, "Popen", spawn)
    monkeypatch.setattr(admin_process, "_rss", lambda pid: 100)
    try:
        if phase in {"before", "running", "exited"}:
            with pytest.raises(LakeError, match="lake_fence_lost"):
                admin_process.run_admin(
                    {"operation": "publish"}, tmp_path, check_fence=check
                )
        else:
            job_request = (
                {"operation": "deprecate_legacy", "apply": False}
                if phase == "readonly"
                else {"operation": "publish"}
            )
            options = {} if phase == "readonly" else {"check_fence": check}
            assert admin_process.run_admin(job_request, tmp_path, **options)[0] == {
                "verified": True
            }
        if phase == "before":
            assert not children
        else:
            child = children[0]
            assert child.returncode is not None
            with pytest.raises(ChildProcessError):
                os.waitpid(child.pid, os.WNOHANG)
            if phase == "running":
                assert child.returncode == -9
        assert not (tmp_path / "admin-request.json").exists()
        assert not (tmp_path / "admin-reply.json").exists()
    finally:
        parent.close()
        peer.close()
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=3)
