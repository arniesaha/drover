#!/usr/bin/env python3
"""Mint a GitHub App installation token for agent writes (drover-bot)."""

# Installed Drover imports the packaged helper.  This fallback only preserves
# direct ``python scripts/github_app_token.py`` use from a source checkout.
try:
    from drover.github_app_token import main
except ModuleNotFoundError:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from drover.github_app_token import main

if __name__ == "__main__":
    raise SystemExit(main())
