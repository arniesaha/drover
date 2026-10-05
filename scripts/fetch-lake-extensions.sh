#!/usr/bin/env bash
# Download the pinned DuckLake + postgres_scanner artifacts into DIR and verify
# them against the hashes pinned in src/drover/server/lake/runtime.py.
# Usage: scripts/fetch-lake-extensions.sh DIR   (then export DROVER_TEST_LAKE_EXTENSIONS=DIR)
set -euo pipefail

dir="${1:?usage: fetch-lake-extensions.sh DIR}"
cd "$(dirname "$0")/.."
PY="python"
if [[ -x ".venv/bin/python" ]]; then PY=".venv/bin/python"; fi

mkdir -p "$dir"
"$PY" - "$dir" <<'PYEOF'
import gzip
import subprocess
import sys
from pathlib import Path

import duckdb

from drover.server.lake.runtime import EXTENSION_HASHES, sha256_file

target = Path(sys.argv[1])
with duckdb.connect(config={"autoload_known_extensions": False}) as con:
    platform = con.execute("PRAGMA platform").fetchone()[0]
for name, expected in EXTENSION_HASHES[platform].items():
    path = target / f"{name}.duckdb_extension"
    if path.is_file() and sha256_file(path) == expected:
        continue
    url = f"https://extensions.duckdb.org/v{duckdb.__version__}/{platform}/{name}.duckdb_extension.gz"
    # The extension CDN rejects Python urllib's default user agent (HTTP 403).
    response = subprocess.run(
        [
            "curl",
            "--fail",
            "--location",
            "--silent",
            "--show-error",
            "--max-time",
            "120",
            url,
        ],
        check=True,
        stdout=subprocess.PIPE,
    )
    path.write_bytes(gzip.decompress(response.stdout))
    if sha256_file(path) != expected:
        path.unlink()
        raise SystemExit(f"{name}: downloaded artifact does not match the pinned hash")
print(f"lake extensions ready in {target} ({platform})")
PYEOF
