#!/usr/bin/env python3
"""Smoke-check that an installed wheel contains the static web-client bundle."""

from __future__ import annotations

import argparse
import subprocess
import tempfile
from pathlib import Path
from typing import Sequence

CHECK = r"""
from drover.server.web.ui import client_assets, load_client_asset

assets = client_assets()
assert "index.html" in assets
shell, body = load_client_asset("index.html")
assert shell.content_type == "text/html; charset=utf-8"
assert shell.cache_control == "no-store"
assert b'<div id="app"></div>' in body
assert any(name.startswith("assets/") for name in assets)
"""


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, required=True, dest="runtime_python")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="drover-web-client-wheel-") as directory:
        subprocess.run(
            [str(args.runtime_python), "-c", CHECK], check=True, cwd=directory
        )
    print("installed wheel contains the web client bundle")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
