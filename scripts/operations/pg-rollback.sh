#!/bin/zsh
set -eu
# Default: verify saved state. Explicit --execute restores manual supervision.
exec /opt/homebrew/bin/python3 /Users/arnab/.drover/maintenance/studio_pg_supervision.py rollback "$@"
