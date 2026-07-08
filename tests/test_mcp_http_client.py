from __future__ import annotations

import json
from typing import Any

from click.testing import CliRunner

from drover.server.__main__ import main
from drover.server.mcp import client


class _Response:
    def __init__(
        self,
        payload: dict[str, Any],
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {"content-type": "text/event-stream"}
        self.text = f"event: message\ndata: {json.dumps(payload)}\n\n"
        self.ok = 200 <= status_code < 300

    def json(self) -> dict[str, Any]:
        return json.loads(self.text)


def test_call_tool_uses_streamable_http_session(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_post(url, *, headers, json, timeout, allow_redirects):
        calls.append({"url": url, "headers": headers, "json": json})
        method = json["method"]
        if method == "initialize":
            return _Response(
                {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "x"}},
                headers={
                    "content-type": "text/event-stream",
                    "mcp-session-id": "session-1",
                },
            )
        if method == "notifications/initialized":
            return _Response({"jsonrpc": "2.0", "result": {}})
        return _Response(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "result": {
                    "content": [],
                    "structuredContent": {"result": {"ok": True}},
                    "isError": False,
                },
            }
        )

    monkeypatch.setattr(client.requests, "post", fake_post)

    out = client.call_tool(
        "http://nexus.example/mcp/",
        "drover_project_brief",
        {"project_key": "arniesaha/mirador"},
    )

    assert out["structuredContent"]["result"] == {"ok": True}
    assert calls[0]["url"] == "http://nexus.example/mcp"
    assert calls[-1]["headers"]["Mcp-Session-Id"] == "session-1"
    assert calls[-1]["json"]["params"]["name"] == "drover_project_brief"
    assert calls[-1]["json"]["params"]["arguments"] == {
        "project_key": "arniesaha/mirador"
    }


def test_cli_mcp_call_prints_result(monkeypatch, tmp_path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(f"""
        [paths]
        incoming_dir = "{tmp_path / 'incoming'}"
        parquet_dir = "{tmp_path / 'parquet'}"
        duckdb_path = "{tmp_path / 'nexus.duckdb'}"

        [server]
        mcp_http_port = 17077

        [agent]
        agent_id = "test"
        principal_id = "arnab"
        """)

    def fake_call_tool(url, name, arguments, *, timeout):
        return {
            "url": url,
            "name": name,
            "arguments": arguments,
            "timeout": timeout,
        }

    monkeypatch.setattr(client, "call_tool", fake_call_tool)
    runner = CliRunner()
    res = runner.invoke(
        main,
        [
            "--config",
            str(cfg),
            "mcp",
            "call",
            "drover_recent_sessions",
            "--arg",
            "project_key=arniesaha/mirador",
            "--arg",
            "limit=5",
        ],
    )

    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["url"] == "http://127.0.0.1:17077/mcp"
    assert payload["name"] == "drover_recent_sessions"
    assert payload["arguments"] == {"project_key": "arniesaha/mirador", "limit": 5}
