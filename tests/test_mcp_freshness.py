import asyncio
import json

from drover.schema import bootstrap
from drover.server.mcp import tools
from drover.server.mcp.contract import READ_CAPS, serialized_bytes
from drover.server.mcp.freshness import with_freshness
from drover.server.mcp.server import build_mcp_server
from drover.server.recall_bundle import _project_session_summary


def test_watermarks_are_data_times_and_per_item_hosts():
    value = with_freshness(
        {
            "results": [
                {
                    "session_id": "old",
                    "agent_id": "host-a",
                    "generated_at": "2026-09-30T01:00:00Z",
                    "ended_at": "2026-10-01T00:00:00Z",
                },
                {
                    "session_id": "new",
                    "host_id": "host-b",
                    "timestamp": "2026-10-01T03:00:00-07:00",
                },
            ],
            "retrieval_timestamp": "2099-01-01T00:00:00Z",
        }
    )
    assert value["store"] == "hub"
    assert value["host"]
    assert value["data_watermark"]["timestamp"] == "2026-10-01T10:00:00+00:00"
    old, new = value["results"]
    assert old["host"] == "host-a"
    assert old["data_watermark"] == {
        "timestamp": "2026-09-30T01:00:00+00:00",
        "basis": "summary_generated_at",
    }
    assert new["host"] == "host-b"
    assert new["data_watermark"]["basis"] == "event_time"


def test_bundle_summary_uses_generation_time_and_producer():
    item = _project_session_summary(
        {
            "session_id": "s",
            "agent_id": "host-a",
            "summary_md": "work",
            "generated_at": "2026-09-30T01:00:00Z",
            "ended_at": "2026-10-01T00:00:00Z",
        },
        retrieval_timestamp="2099-01-01T00:00:00Z",
        join_basis="test",
    )
    bundle = with_freshness({"drover_context": {"summaries": [item]}})
    assert item["store"] == "hub"
    assert item["host"] == "host-a"
    assert item["data_watermark"]["basis"] == "summary_generated_at"
    assert bundle["data_watermark"]["timestamp"] == "2026-09-30T01:00:00+00:00"


def test_unknown_freshness_is_explicit():
    result = with_freshness(
        {"results": [], "retrieval_timestamp": "2099-01-01T00:00:00Z"}
    )
    assert result["data_watermark"] == {"timestamp": None, "basis": "unknown"}
    assert with_freshness(None) is None
    assert with_freshness(None, empty_envelope=True)["status"] == "unavailable"


def test_every_public_read_carries_freshness_even_without_data(tmp_path, monkeypatch):
    path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=path)
    for name in READ_CAPS:
        if hasattr(tools, name):
            monkeypatch.setattr(tools, name, lambda **kw: None)
    server = build_mcp_server(duckdb_path=path)
    for tool in asyncio.run(server.list_tools()):
        if tool.name not in READ_CAPS:
            continue
        args = {key: "test" for key in tool.inputSchema.get("required", [])}
        if "harness_ids" in args:
            args["harness_ids"] = ["test"]
        if tool.name == "drover_recall_bundle":
            # Bundle requires search-shaped empty data even when memory is absent.
            monkeypatch.setattr(
                "drover.server.recall_bundle.drover_search",
                lambda **kw: {"results": []},
            )
        content = asyncio.run(server.call_tool(tool.name, args))
        if isinstance(content, tuple):
            content = content[0]
        result = json.loads(content[0].text)
        assert result["store"] == "hub", tool.name
        assert result["host"], tool.name
        assert result["data_watermark"] == {
            "timestamp": None,
            "basis": "unknown",
        }, tool.name
        assert serialized_bytes(result) <= READ_CAPS[tool.name].response_bytes


def test_active_handoff_uses_saved_brief_generation_time():
    result = with_freshness({"session_id": "s", "freshness_ts": "2026-10-01T00:00:00Z"})
    assert result["data_watermark"] == {
        "timestamp": "2026-10-01T00:00:00+00:00",
        "basis": "brief_generated_at",
    }
