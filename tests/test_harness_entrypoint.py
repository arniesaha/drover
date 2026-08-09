"""Import-footprint checks for the standalone harnessd entry point."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap


def test_skinny_harnessd_import_avoids_server_wide_modules():
    script = textwrap.dedent("""
        import json
        import sys

        import drover.server.harness.cli  # noqa: F401

        loaded = {
            name: name in sys.modules
            for name in [
                "pyarrow",
                "grpc",
                "mcp",
                "anthropic",
                "drover.server.providers.types",
                "drover.server.summarizer.worker",
                "drover.server.embeddings.worker",
                "drover.server.otlp.receiver",
                "drover.server.mcp.server",
            ]
        }
        print(json.dumps(loaded, sort_keys=True))
        """)

    result = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )

    loaded = json.loads(result.stdout)
    assert loaded == {name: False for name in loaded}
