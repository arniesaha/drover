"""Export the existing Python owner contract for the standalone Node plugin.

Run from the Drover worktree with its normal development Python environment.
No OpenClaw installation/configuration or runtime activity is performed.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from drover.server.harness.openclaw_owner import OwnerToolCall, OwnerToolReply


def export(*, check: bool = False) -> None:
    directory = (
        Path(__file__).resolve().parents[1]
        / "plugins/openclaw-continuity-owner/schemas"
    )
    for filename, model in (
        ("request.json", OwnerToolCall),
        ("reply.json", OwnerToolReply),
    ):
        content = json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n"
        path = directory / filename
        if check:
            if not path.exists() or path.read_text() != content:
                raise SystemExit(f"stale exported owner schema: {path}")
        else:
            directory.mkdir(parents=True, exist_ok=True)
            path.write_text(content)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    export(check=parser.parse_args().check)
