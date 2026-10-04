"""Real exec'd writers share only private fixture files and throwaway PG."""

import json
import os
import selectors
import subprocess
import sys
import threading
from contextlib import contextmanager
from dataclasses import asdict, replace

import pytest
from test_control_outbox import postgres_control_store
from test_lake_runtime import lake_spec
from test_lake_serving import verified_lake

from drover.server.control_store import control_store_config
from drover.server.lake.runtime import LakeError
from drover.server.lake.serving import configure_analytics
from drover.server.lake.writer_gate import activate_retirement

_CHILD = r"""
import json, sys
from pathlib import Path
from drover import schema
from drover.config import AnalyticsConfig, ControlStoreConfig
from drover.server.control_store import configure_control_store
from drover.server.lake.serving import configure_analytics
from drover.server.lake.writer_gate import legacy_derived_write
from drover.server.memory_identity import refresh_memory_projection
from drover.server.native_usage_rollup import rollup_pending_native_usage

settings = json.loads(sys.argv[1])
path = Path(settings["path"])
if settings["control"] is not None:
    configure_control_store(path, ControlStoreConfig(**settings["control"]))
if settings["analytics"] is not None:
    configure_analytics(path, AnalyticsConfig(**settings["analytics"]))
if settings["pause"]:
    original = schema.open_duckdb_connection
    def open_paused(*args, **kwargs):
        con = original(*args, **kwargs)
        print(json.dumps({"event": "entered"}), flush=True)
        assert json.loads(sys.stdin.readline())["action"] == "release"
        schema.open_duckdb_connection = original
        return con
    schema.open_duckdb_connection = open_paused
print(json.dumps({"event": "ready"}), flush=True)
for line in sys.stdin:
    command = json.loads(line)
    if command["action"] == "exit":
        break
    try:
        if command["action"] == "bootstrap":
            schema.bootstrap(duckdb_path=path, parquet_dir=path.parent / command["directory"])
            result = None
        elif command["action"] == "nested":
            with legacy_derived_write(path) as allowed:
                result = schema.backfill_agent_event_day_summary(path) if allowed else 0
        elif command["action"] == "day":
            result = schema.backfill_agent_event_day_summary(path)
        elif command["action"] == "projection":
            result = refresh_memory_projection(None, {}, store_path=path)
        elif command["action"] == "usage":
            result = rollup_pending_native_usage(path).partitions
        print(json.dumps({"result": result}), flush=True)
    except Exception as exc:
        print(json.dumps({"error": getattr(exc, "code", type(exc).__name__)}), flush=True)
"""


@pytest.fixture(autouse=True)
def private_selection_registry(monkeypatch):
    # pytest may recycle a passed parameter's long-name tmp directory. Each
    # fixture namespace gets its own local config registry; cross-process latch
    # assertions still use the real persisted inode, never mocked retirement.
    from drover.server.lake import serving

    monkeypatch.setattr(serving, "_CONFIGS", {})


class Writer:
    def __init__(self, process):
        self.process = process

    def send(self, action, **arguments):
        self.process.stdin.write(json.dumps(dict(action=action, **arguments)) + "\n")
        self.process.stdin.flush()

    def receive(self):
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            assert selector.select(30), "private writer process timed out"
        line = self.process.stdout.readline()
        assert line, f"writer exited: {self.process.poll()}"
        return json.loads(line)

    def call(self, action, **arguments):
        self.send(action, **arguments)
        return self.receive()


@contextmanager
def writer_process(path, *, config=None, control=None, pause=False):
    settings = dict(
        path=str(path),
        analytics=asdict(config) if config else None,
        control=asdict(control) if control else None,
        pause=pause,
    )
    # Fresh interpreter: no inherited locks, _CONFIGS, retirement maps or PG pool.
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", _CHILD, json.dumps(settings)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=dict(os.environ),
    )
    writer = Writer(process)
    try:
        assert writer.receive() == {"event": "ready"}
        yield writer
        writer.send("exit")
        _, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)


def test_exec_legacy_default_bootstrap_really_writes(tmp_path):
    path = tmp_path / "legacy.duckdb"
    with writer_process(path) as writer:
        assert writer.call("bootstrap", directory="legacy-parquet") == {"result": None}
        # Exercise the nested gate used by the actual day-summary worker too.
        assert writer.call("nested")["result"] == 0
    assert path.exists()
    assert list((tmp_path / "legacy-parquet").rglob("*.parquet"))


def test_retirement_drains_real_decorated_process_then_blocks_other_writers(
    verified_lake,
):
    _, path, config = verified_lake
    config = replace(config, retire_legacy_writers=True)
    control = control_store_config(path)
    activated = threading.Event()
    starting = threading.Event()
    failures = []

    def activate():
        starting.set()
        try:
            activate_retirement(path)
            activated.set()
        except BaseException as exc:
            failures.append(exc)

    # Parent selection is opt-in; independent writer still has legacy defaults.
    configure_analytics(path, config)
    with writer_process(path, control=control, pause=True) as writer:
        writer.send("bootstrap", directory="inflight-parquet")
        assert writer.receive() == {"event": "entered"}
        retiring = threading.Thread(target=activate)
        retiring.start()
        try:
            assert starting.wait(3)
            assert not activated.wait(
                0.2
            ), "activation overtook another process's write"
            writer.send("release")
            assert writer.receive() == {"result": None}
        finally:
            retiring.join(30)
        assert not retiring.is_alive() and activated.is_set() and not failures
        assert list((path.parent / "inflight-parquet").rglob("*.parquet"))
        # This already-running process never receives the selected config.
        assert writer.call("bootstrap", directory="blocked-parquet") == {
            "error": "lake_retirement_renewal_required"
        }
    assert not (path.parent / "blocked-parquet").exists()
    # A newly exec'd process cannot forget the latch either.
    with writer_process(path) as writer:
        assert writer.call("day") == {"error": "lake_retirement_renewal_required"}
    with writer_process(path, config=config, control=control) as writer:
        assert writer.call("bootstrap", directory="retired-parquet") == {"result": None}
        assert writer.call("day") == {"result": 0}
        assert writer.call("projection") == {"result": []}
        assert writer.call("usage") == {"result": 0}
    assert not (path.parent / "retired-parquet").exists()


def test_other_process_cannot_reuse_activation_after_selection_and_epoch_change(
    verified_lake,
):
    _, path, config = verified_lake
    config = replace(config, retire_legacy_writers=True)
    configure_analytics(path, config)
    activate_retirement(path)
    with writer_process(
        path, config=config, control=control_store_config(path)
    ) as writer:
        assert writer.call("day") == {"result": 0}
        renewed = replace(config, epoch="next-epoch")
        configure_analytics(path, renewed)
        assert writer.call("day") == {"error": "lake_retirement_renewal_required"}
        # Changing back does not resurrect the former active receipt.
        configure_analytics(path, config)
        assert writer.call("day") == {"error": "lake_retirement_renewal_required"}
        activate_retirement(path)
        assert writer.call("day") == {"result": 0}
        from drover.config import AnalyticsConfig

        configure_analytics(path, AnalyticsConfig())
        assert writer.call("day") == {"error": "lake_retirement_renewal_required"}


@pytest.mark.parametrize("proof_state", ["missing", "corrupt"])
def test_missing_activation_and_failed_verification_latch_other_processes_closed(
    verified_lake, proof_state
):
    spec, path, config = verified_lake
    config = replace(config, retire_legacy_writers=True)
    configure_analytics(path, config)
    control = control_store_config(path)
    with writer_process(path, config=config, control=control) as writer:
        assert writer.call("bootstrap", directory="no-activation") == {
            "error": "lake_retirement_not_activated"
        }
    proof = spec.data_root / "verification/serving-proof.json"
    if proof_state == "missing":
        proof.unlink()
    else:
        proof.write_text("{}")
    with pytest.raises(LakeError, match="verification"):
        activate_retirement(path)
    with writer_process(path, config=config, control=control) as writer:
        assert writer.call("bootstrap", directory="failed-proof") == {
            "error": "lake_retirement_renewal_required"
        }
    with writer_process(path) as writer:
        assert writer.call("day") == {"error": "lake_retirement_renewal_required"}
    assert not (path.parent / "failed-proof").exists()
    assert not (path.parent / "no-activation").exists()


def test_activated_other_process_rechecks_lost_verification(verified_lake):
    spec, path, config = verified_lake
    config = replace(config, retire_legacy_writers=True)
    configure_analytics(path, config)
    activate_retirement(path)
    with writer_process(
        path, config=config, control=control_store_config(path)
    ) as writer:
        assert writer.call("day") == {"result": 0}
        (spec.data_root / "verification/serving-proof.json").write_text("{}")
        result = writer.call("bootstrap", directory="lost-proof")
        assert "verification" in result["error"]
    assert not (path.parent / "lost-proof").exists()


def test_distinct_exporter_paths_and_corrupt_durable_state_fail_closed(verified_lake):
    from drover.server.control_exporter import ControlOutboxExporter
    from drover.server.lake.writer_gate import _fence_path

    _, path, config = verified_lake
    configure_analytics(path, replace(config, retire_legacy_writers=True))
    activate_retirement(path)
    exporter = ControlOutboxExporter(
        control_path=path.parent / "separate-control.duckdb",
        analytical_path=path,
        parquet_dir=path.parent / "not-created",
    )
    assert not exporter.run_once()["enabled"]
    # Partial/crashed latch writes are never interpreted as a legacy default.
    _fence_path(path).write_bytes(b'{"version":')
    with writer_process(path) as writer:
        assert writer.call("day") == {"error": "lake_retirement_state_invalid"}
