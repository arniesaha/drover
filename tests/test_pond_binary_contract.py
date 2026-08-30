"""Drover's pinned client against the real pinned Pond binary.

The unit suite proves the client against a fake HTTP server that encodes what
we believe Pond v0.16.3 says on the wire. That belief is exactly what an
upstream release can silently change, so CI also runs this file against the
real binary (`POND_BINARY` set): if the envelope or local copy contract drifts,
the contract breaks here first, not on an operator's hub.

Skipped locally unless POND_BINARY points at a pond executable.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from drover.config import ArchiveConfig
from drover.server.archive import PondArchiveClient, export_pond_inventory
from drover.server.archive.errors import ArchiveError, ArchiveRequestRejected
from drover.server.archive.inventory import load_pond_inventory
from drover.server.archive.pond_process import (
    _pin_pond_executable,
    _PinnedPondExecutable,
    run_pond_process,
)
from drover.server.archive.pond_snapshot import (
    LocalPondStore,
    PondStoreSnapshot,
    _capture_pond_store_snapshot,
)
from drover.server.archive.types import ArchiveMessageRequest, ArchiveSearchRequest

POND_BINARY = os.environ.get("POND_BINARY")

pytestmark = pytest.mark.skipif(
    not POND_BINARY, reason="POND_BINARY not set; real-binary contract runs in CI"
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass(frozen=True)
class PondRuntime:
    client: PondArchiveClient
    env: dict[str, str]
    storage_path: Path


@pytest.fixture(scope="module")
def pond_runtime(tmp_path_factory):
    home = tmp_path_factory.mktemp("pond")
    config = home / "config.toml"
    storage_path = home / "store"
    env = dict(
        os.environ,
        POND_CONFIG_FILE=str(config),
        POND_STORAGE_PATH=str(storage_path),
    )
    subprocess.run(
        [POND_BINARY, "init", "--yes", "--skip-mcp", "--adapters", ""],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    port = _free_port()
    proc = subprocess.Popen(
        [POND_BINARY, "serve", "--transport", "http", "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    client = PondArchiveClient(
        ArchiveConfig(
            enabled=True,
            base_url=base,
            timeout_seconds=3.0,
            search_limit=5,
            context_before=2,
            context_after=2,
            max_context_chars=24000,
            max_response_bytes=1048576,
        )
    )
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            client.search(ArchiveSearchRequest(query="warmup"))
            break
        except ArchiveError as exc:
            last = exc
            time.sleep(0.5)
    else:
        proc.terminate()
        pytest.fail(f"pond serve never answered: {last}")
    yield PondRuntime(client=client, env=env, storage_path=storage_path)
    proc.terminate()
    proc.wait(timeout=10)


def test_the_pinned_binary_is_the_pinned_version():
    out = subprocess.run(
        [POND_BINARY, "--version"], capture_output=True, text=True, timeout=30
    )
    assert "0.16.3" in out.stdout, out.stdout


def test_search_on_an_empty_store_returns_an_empty_result(pond_runtime):
    result = pond_runtime.client.search(ArchiveSearchRequest(query="anything at all"))
    assert result.hits == ()


def test_search_honors_scoping_parameters(pond_runtime):
    result = pond_runtime.client.search(
        ArchiveSearchRequest(query="scoped", project="drover", limit=3)
    )
    assert result.hits == ()


def test_get_message_for_an_unknown_id_is_a_typed_rejection(pond_runtime):
    with pytest.raises((ArchiveRequestRejected, ArchiveError)):
        pond_runtime.client.get_message(
            ArchiveMessageRequest(
                message_id="00000000-0000-0000-0000-000000000000",
                context_before=1,
                context_after=1,
            )
        )


def test_pinned_binary_exports_the_inventory_sql_contract(pond_runtime, tmp_path):
    output = tmp_path / "pond-inventory.json"

    inventory = export_pond_inventory(
        Path(POND_BINARY),
        output,
        storage_path=pond_runtime.storage_path,
        env=pond_runtime.env,
    )

    assert inventory.pond_version == "0.16.3"
    assert inventory.records == ()
    assert load_pond_inventory(output) == inventory


def _private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700)
    return path


def _seed_one_canonical_session(
    executable: _PinnedPondExecutable,
    tmp_path: Path,
) -> tuple[Path, Path]:
    home = _private_directory(tmp_path / "contract-home")
    project = home / ".claude" / "projects" / "-contract-project"
    project.mkdir(parents=True)
    session_id = "11111111-1111-4111-8111-111111111111"
    user_id = "22222222-2222-4222-8222-222222222222"
    assistant_id = "33333333-3333-4333-8333-333333333333"
    rows = (
        {
            "parentUuid": None,
            "isSidechain": False,
            "message": {"role": "user", "content": "contract question"},
            "type": "user",
            "uuid": user_id,
            "timestamp": "2026-08-29T12:00:00.000Z",
            "cwd": "/contract-project",
            "sessionId": session_id,
        },
        {
            "parentUuid": user_id,
            "isSidechain": False,
            "message": {
                "model": "claude-contract",
                "role": "assistant",
                "content": [{"type": "text", "text": "contract answer"}],
            },
            "type": "assistant",
            "uuid": assistant_id,
            "timestamp": "2026-08-29T12:00:01.000Z",
            "cwd": "/contract-project",
            "sessionId": session_id,
        },
    )
    transcript = project / f"{session_id}.jsonl"
    transcript.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )

    config = home / "config.toml"
    source_store = home / "source-store"
    environment = dict(
        os.environ,
        HOME=str(home),
        POND_CONFIG_FILE=str(config),
        POND_STORAGE_PATH=str(source_store),
    )
    initialized = subprocess.run(
        [
            str(executable.path),
            "init",
            "--yes",
            "--skip-mcp",
            "--adapters",
            "claude-code",
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert initialized.returncode == 0, initialized.stdout + initialized.stderr
    config.chmod(0o600)
    source_store.mkdir(mode=0o700, exist_ok=True)
    source_store.chmod(0o700)

    sync_directory = _private_directory(tmp_path / "sync-process")
    synced = run_pond_process(
        executable,
        (
            "--config-file",
            str(config),
            "--storage-path",
            str(source_store),
            "sync",
        ),
        timeout_seconds=60,
        run_directory=sync_directory,
        label="sync",
        env={"HOME": str(home)},
    )
    assert synced.returncode == 0
    return config, source_store


def _copy_local_generation(
    executable: _PinnedPondExecutable,
    config: Path,
    source: Path,
    destination: Path,
    workspace: Path,
    *,
    verify_only: bool = False,
) -> None:
    arguments = ["--config-file", str(config), "copy"]
    if verify_only:
        arguments.append("--verify-only")
    arguments.extend(("--from", str(source), "--to", str(destination)))
    result = run_pond_process(
        executable,
        tuple(arguments),
        timeout_seconds=60,
        run_directory=_private_directory(workspace),
        label="verify-only" if verify_only else "copy",
    )
    assert result.returncode == 0


def _capture_local_contract_snapshot(
    executable: _PinnedPondExecutable,
    config: Path,
    store: Path,
    workspace: Path,
) -> PondStoreSnapshot:
    return _capture_pond_store_snapshot(
        executable,
        storage=LocalPondStore(store),
        pond_config=config,
        workspace=_private_directory(workspace),
        timeout_seconds=60,
        release_evidence=None,
    )


def test_pinned_binary_copies_verifies_and_restores_one_generation(tmp_path):
    binary = Path(POND_BINARY)
    generation_store = tmp_path / "generation"
    restore_store = tmp_path / "restore"
    generation_store.mkdir(mode=0o700)
    restore_store.mkdir(mode=0o700)

    with _pin_pond_executable(binary) as executable:
        config, source_store = _seed_one_canonical_session(executable, tmp_path)
        _copy_local_generation(
            executable,
            config,
            source_store,
            generation_store,
            tmp_path / "generation-copy",
        )
        _copy_local_generation(
            executable,
            config,
            source_store,
            generation_store,
            tmp_path / "generation-verify",
            verify_only=True,
        )
        _copy_local_generation(
            executable,
            config,
            generation_store,
            restore_store,
            tmp_path / "restore-copy",
        )
        source = _capture_local_contract_snapshot(
            executable,
            config,
            source_store,
            tmp_path / "source-snapshot",
        )
        restored = _capture_local_contract_snapshot(
            executable,
            config,
            restore_store,
            tmp_path / "restore-snapshot",
        )

    assert len(source.root_inventory.records) == 1
    assert source.root_inventory.records[0].source_agent == "claude-code"
    assert source.counts.sessions == 1
    assert source.counts.messages == 2
    assert source.counts.parts == 2
    assert restored.root_inventory.records == source.root_inventory.records
    assert restored.counts == source.counts
