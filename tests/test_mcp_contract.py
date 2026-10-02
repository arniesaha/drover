"""Exercise the public registry: a newly registered read must declare caps."""

import asyncio
import json
import threading
from dataclasses import replace

import pytest

from drover.server.mcp import tools
from drover.server.mcp.contract import READ_CAPS, ReadAdmission, serialized_bytes
from drover.server.mcp.server import build_mcp_server
from drover.server.recall_bundle import RecallBundleService


def call(server, name, arguments):
    content = asyncio.run(server.call_tool(name, arguments))
    if isinstance(content, tuple):
        content = content[0]
    return json.loads(content[0].text)


def test_every_registered_read_enforces_caps(tmp_path, monkeypatch):
    seen = {}

    def implementation(name):
        def run(**kwargs):
            seen[name] = kwargs
            return {
                "results": [{"content": "界" * 10000, "metadata": '\\"' * 10000}] * 500,
                "metadata": {str(i): "x" * 10000 for i in range(30)},
            }

        return run

    for name in READ_CAPS:
        if hasattr(tools, name):
            monkeypatch.setattr(tools, name, implementation(name))
    monkeypatch.setattr(
        RecallBundleService,
        "recall_bundle",
        lambda self, **kw: implementation("drover_recall_bundle")(**kw),
    )
    server = build_mcp_server(duckdb_path=tmp_path / "unused")
    registered = asyncio.run(server.list_tools())
    assert {t.name for t in registered} - {"drover_session_close"} == set(READ_CAPS)
    for tool in registered:
        if tool.name == "drover_session_close":
            continue
        caps = READ_CAPS[tool.name]
        args = {key: "test" for key in tool.inputSchema.get("required", [])}
        if "harness_ids" in args:
            args["harness_ids"] = ["test"] * 26
            with pytest.raises(Exception, match="at most 25"):
                call(server, tool.name, args)
            args["harness_ids"] = ["test"] * 25
        oversized = False
        for key in (
            "limit",
            "max_summaries",
            "last_n_turns",
            "max_artifacts",
            "max_projects",
        ):
            if key in tool.inputSchema["properties"]:
                args[key] = 1000000
                oversized = True
        result = call(server, tool.name, args)
        assert result["truncated"] is True
        assert serialized_bytes(result) <= caps.response_bytes

        def check(value):
            if isinstance(value, str):
                assert len(value.encode()) <= caps.text_bytes
            elif isinstance(value, list):
                assert len(value) <= caps.rows
                for child in value:
                    check(child)
            elif isinstance(value, dict):
                for child in value.values():
                    check(child)

        check(result)
        if oversized:
            for key in args.keys() & seen[tool.name].keys():
                if key in {
                    "limit",
                    "max_summaries",
                    "last_n_turns",
                    "max_artifacts",
                    "max_projects",
                }:
                    assert seen[tool.name][key] <= caps.rows


def test_deadline_retains_admission_until_work_finishes(monkeypatch):
    started, finish = threading.Event(), threading.Event()

    def drover_search(query="test", limit=1):
        started.set()
        finish.wait(2)
        return {"results": []}

    monkeypatch.setitem(
        READ_CAPS,
        "drover_search",
        replace(READ_CAPS["drover_search"], deadline_seconds=0.03),
    )
    read = ReadAdmission(concurrency=1).wrap(drover_search)

    async def exercise():
        result = await read()
        assert started.is_set()
        assert result["status"] == "timeout"
        assert (await read())["status"] == "busy"

    try:
        asyncio.run(exercise())
    finally:
        finish.set()


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_invalid_limits_rejected_before_storage(tmp_path, limit):
    with pytest.raises(ValueError, match="positive integer"):
        tools.drover_search(duckdb_path=tmp_path / "unused", query="x", limit=limit)


def test_embedding_errors_even_without_memory(tmp_path):
    with pytest.raises(ValueError, match="dimensions"):
        tools.drover_recall(
            duckdb_path=tmp_path / "unused",
            query_embedding=[1.0],
            embedding_model="hub",
        )
    with pytest.raises(ValueError, match="model"):
        tools.drover_recall(
            duckdb_path=tmp_path / "unused",
            query_embedding=[1.0] * 768,
            embedding_model="hub",
            query_embedding_model="wrong",
        )


def test_fleet_reads_registry_without_analytical_connection(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from drover.server.harness.models import HarnessSession
    from drover.server.harness.registry import HarnessRegistry

    monkeypatch.setattr(tools, "_connect", lambda *a: pytest.fail("analytical read"))
    monkeypatch.setattr(
        HarnessRegistry, "list_hosts", lambda self: [SimpleNamespace(host_id="live")]
    )
    monkeypatch.setattr(
        HarnessRegistry,
        "list_sessions",
        lambda self, **kw: [
            HarnessSession("running", "live", "codex", "codex", "running"),
            HarnessSession("retired", "retired", "codex", "codex", "running"),
        ],
    )
    for read in (tools.drover_fleet_status, tools.drover_active_sessions):
        result = read(duckdb_path=tmp_path / "unused")
        assert result["authoritative"] is True
        assert result["state_source"] == "control_plane.harness_sessions+harness_hosts"
        assert [s["session_id"] for s in result["active_sessions"]] == ["running"]
