"""Copyable Claude Code SessionStart hook; requires Drover in this Python env."""

import json
import os
import sys

from drover.server.mcp.client import call_tool


def main():
    try:
        result = call_tool(
            os.environ["DROVER_MCP_URL"],
            "drover_profile",
            {"scope": "first_turn"},
            timeout=5,
        )
        if result.get("isError"):
            raise ValueError("MCP tool error")
        value = json.loads(
            next(c["text"] for c in result["content"] if c["type"] == "text")
        )
        if value.get("status") in {"busy", "timeout", "error", "unavailable"}:
            raise ValueError("profile unavailable")
        context = json.dumps(
            {
                "profile_context": value["bundle"],
                "data_watermark": value["data_watermark"],
                "oldest_item_age_seconds": value["oldest_item_age_seconds"],
            },
            ensure_ascii=False,
        )
    except Exception:
        # Never inject a failed response or log remote content or credentials.
        print("Drover profile unavailable for this session.", file=sys.stderr)
        return
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": context,
                }
            }
        )
    )


if __name__ == "__main__":
    main()
