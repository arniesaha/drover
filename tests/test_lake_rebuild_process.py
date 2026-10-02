"""Offline job supervision fails closed and always reaps its child."""

from types import SimpleNamespace

import pytest

from drover.server.lake import admin_process, rebuild_worker
from drover.server.lake.runtime import LakeError


@pytest.mark.parametrize("breach", ["rss", "deadline"])
def test_admin_ceiling_kills_and_reaps(tmp_path, monkeypatch, breach):
    child = SimpleNamespace(pid=123, returncode=None)
    child.poll = lambda: child.returncode
    reaped = []
    child.wait = lambda: reaped.append(True)
    killed = []

    def kill(pid, sig):
        killed.append((pid, sig))
        child.returncode = -9

    monkeypatch.setattr(admin_process.subprocess, "Popen", lambda *a, **kw: child)
    monkeypatch.setattr(admin_process.os, "killpg", kill)
    monkeypatch.setattr(
        admin_process,
        "_rss",
        lambda pid: admin_process.RSS_CEILING if breach == "rss" else 100,
    )
    ticks = iter([0, 1801])
    monkeypatch.setattr(admin_process.time, "monotonic", lambda: next(ticks))
    code = "rss_limit_exceeded" if breach == "rss" else "partition_deadline_exceeded"
    with pytest.raises(LakeError, match=code):
        admin_process.run_admin({"operation": "other"}, tmp_path)
    assert killed and reaped
    assert not (tmp_path / "admin-request.json").exists()


def test_worker_limits_and_spill_are_explicit(tmp_path, monkeypatch):
    captured = {}

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, statement):
            assert statement == "SET TimeZone = 'UTC'"

    def connect(path, config):
        captured.update(config)
        return Connection()

    monkeypatch.setattr(rebuild_worker.duckdb, "connect", connect)
    monkeypatch.setattr(rebuild_worker, "_other", lambda con, job: {"ok": True})
    spill = tmp_path / "data-volume/spill"
    assert rebuild_worker.work(
        {
            "output": str(tmp_path / "partition"),
            "spill": str(spill),
            "operation": "other",
        }
    ) == {"ok": True}
    assert captured["memory_limit"] == "1GB"
    assert captured["threads"] == 1
    assert captured["temp_directory"] == str(spill)
    assert not captured["autoload_known_extensions"]
    assert not captured["autoinstall_known_extensions"]


def test_exit_transition_without_task_info_is_reaped(tmp_path, monkeypatch):
    import json

    child = SimpleNamespace(pid=123, returncode=None)
    child.poll = lambda: child.returncode

    def wait(timeout=None):
        child.returncode = 0
        (tmp_path / "admin-reply.json").write_text(json.dumps({"verified": True}))

    child.wait = wait
    monkeypatch.setattr(admin_process.subprocess, "Popen", lambda *a, **kw: child)

    def missing(pid):
        raise LakeError("analytics_rss_monitor_unavailable")

    monkeypatch.setattr(admin_process, "_rss", missing)
    assert admin_process.run_admin({}, tmp_path) == ({"verified": True}, 0)
