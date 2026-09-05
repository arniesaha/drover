#!/usr/bin/env bash
# Bounded liveness probe shared by the installer and release checks.
# This is sourced, so it must not alter shell options or run on load.

# wait_for_health <host:port>
wait_for_health() {
  local address="${1%/}" i status
  for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    # Do not use -f alone: it treats 3xx as success.  Do not use -L: a
    # redirect is not proof that this server answers its own health endpoint.
    status="$(curl -sS --max-time 2 -o /dev/null -w '%{http_code}' \
      "http://${address}/healthz" 2>/dev/null)" || status=""
    case "$status" in
      2[0-9][0-9]) return 0 ;;
    esac
    sleep 2
  done
  return 1
}
