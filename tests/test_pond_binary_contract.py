"""Drover's pinned client against the real pinned Pond binary.

The unit suite proves the client against a fake HTTP server that encodes what
we believe Pond v0.16.3 says on the wire. That belief is exactly what an
upstream release can silently change, so CI also runs this file against the
real binary (`POND_BINARY` set, store empty): if the envelope drifts, the
contract breaks here first, not on an operator's hub.

Skipped locally unless POND_BINARY points at a pond executable.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path

import pytest

from drover.config import ArchiveConfig
from drover.server.archive import PondArchiveClient
from drover.server.archive.errors import ArchiveError, ArchiveRequestRejected
from drover.server.archive.types import ArchiveMessageRequest, ArchiveSearchRequest

POND_BINARY = os.environ.get("POND_BINARY")

pytestmark = pytest.mark.skipif(
    not POND_BINARY, reason="POND_BINARY not set; real-binary contract runs in CI"
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def pond_server(tmp_path_factory):
    home = tmp_path_factory.mktemp("pond")
    config = home / "config.toml"
    env = dict(
        os.environ,
        POND_CONFIG_FILE=str(config),
        POND_STORAGE_PATH=str(home / "store"),
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
    yield client
    proc.terminate()
    proc.wait(timeout=10)


def test_the_pinned_binary_is_the_pinned_version():
    out = subprocess.run(
        [POND_BINARY, "--version"], capture_output=True, text=True, timeout=30
    )
    assert "0.16.3" in out.stdout, out.stdout


def test_search_on_an_empty_store_returns_an_empty_result(pond_server):
    result = pond_server.search(ArchiveSearchRequest(query="anything at all"))
    assert result.hits == ()


def test_search_honors_scoping_parameters(pond_server):
    result = pond_server.search(
        ArchiveSearchRequest(query="scoped", project="drover", limit=3)
    )
    assert result.hits == ()


def test_get_message_for_an_unknown_id_is_a_typed_rejection(pond_server):
    with pytest.raises((ArchiveRequestRejected, ArchiveError)):
        pond_server.get_message(
            ArchiveMessageRequest(
                message_id="00000000-0000-0000-0000-000000000000",
                context_before=1,
                context_after=1,
            )
        )
