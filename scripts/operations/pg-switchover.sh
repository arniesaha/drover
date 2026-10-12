#!/bin/zsh
set -eu
# Default: read-only preflight. Only Arnab's scheduled window may use --execute.
exec /opt/homebrew/bin/python3 "$HOME/.drover/maintenance/studio_pg_supervision.py" switch "$@"
