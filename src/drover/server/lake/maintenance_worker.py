"""Child entry point for killable in-hub DuckLake compaction."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .maintenance import compact_lake
from .runtime import LakeError, LakeSpec, sanitize_detail


def main() -> None:
    request = json.loads(Path(sys.argv[1]).read_text())
    reply = Path(request["reply"])
    try:
        spec = LakeSpec(
            request["catalog_dsn_env"],
            Path(request["data_root"]),
            Path(request["extension_dir"]),
            request["engine_sha256"],
        )
        payload = {"report": compact_lake(spec)}
    except LakeError as exc:
        payload = {"error": exc.code, "detail": exc.detail}
    except Exception as exc:  # child reports only sanitized operator detail
        payload = {"error": "lake_compaction_failed", "detail": sanitize_detail(exc)}
    reply.write_text(json.dumps(payload))
    reply.chmod(0o600)


if __name__ == "__main__":
    main()
