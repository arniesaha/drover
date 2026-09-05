#!/usr/bin/env bash
# Bounded liveness probe shared by the installer and release checks.
# This is sourced, so it must not alter shell options or run on load.

# wait_for_health <host:port>
wait_for_health() {
  local address="${1%/}" i
  for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    if curl -fsS --max-time 2 "http://${address}/healthz" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}
