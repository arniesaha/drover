#!/usr/bin/env bash
# Remove native Drover service wiring. Managed PostgreSQL is intentionally retained.
set -euo pipefail
HOME_DIR="${DROVER_HOME:-$HOME/.drover}"
PURGE_MANAGED=0
[ "${1:-}" = '--purge-managed-postgres' ] && PURGE_MANAGED=1
if [ "$PURGE_MANAGED" -eq 1 ] && [ "${2:-}" != '--i-understand-this-deletes-drover-postgres-data' ]; then
  echo 'refusing destructive purge: pass --i-understand-this-deletes-drover-postgres-data' >&2; exit 2
fi
if [ "$(uname -s)" = Darwin ]; then
  for p in "$HOME/Library/LaunchAgents"/com.drover.{server,harnessd}.plist; do [ -e "$p" ] && launchctl unload "$p" 2>/dev/null || true; rm -f "$p"; done
else
  systemctl --user disable --now drover-server.service drover-harnessd.service 2>/dev/null || true
  rm -f "$HOME/.config/systemd/user/drover-server.service" "$HOME/.config/systemd/user/drover-harnessd.service"
  systemctl --user daemon-reload 2>/dev/null || true
fi
rm -f "$HOME/.local/bin/drover-server"
if [ "$PURGE_MANAGED" -eq 1 ]; then
  "$HOME_DIR/bin/drover-managed-postgres" purge --i-understand-this-deletes-drover-postgres-data
else
  echo "Native Drover services removed. Managed PostgreSQL container and data volume were preserved."
fi
