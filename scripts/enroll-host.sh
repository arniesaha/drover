#!/usr/bin/env bash
# Enroll this machine as a Drover relay harness host.
# Usage: ./scripts/enroll-host.sh --host-id work-laptop --central-url http://drover-host.local:7080 --relay
set -euo pipefail

HOST_ID="" CENTRAL_URL="" RELAY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --host-id) HOST_ID="$2"; shift 2 ;;
    --central-url) CENTRAL_URL="$2"; shift 2 ;;
    --relay) RELAY=1; shift ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$HOST_ID" && -n "$CENTRAL_URL" ]] || { echo "need --host-id and --central-url" >&2; exit 2; }

# The shared plist listens on 127.0.0.1 and advertises no --local-url, which is
# correct for a relay host and unreachable for a direct one: the hub would have
# no URL to dial and every request would 502 forever. Rather than install a host
# that can never work, refuse. Direct hosts are enrolled with the pre-existing
# launchd/systemd units until this script grows --local-url/--listen handling.
[[ "$RELAY" == 1 ]] || {
  cat >&2 <<'MSG'
REFUSING: this script only enrolls relay hosts (--relay).

Without --relay the plist it renders listens on 127.0.0.1:7081 and advertises
no URL, so the hub could never reach this machine and every request to it would
return "harness host has no reachable endpoint". Use the existing launchd or
systemd unit for a direct host -- see docs/multi-host.md.
MSG
  exit 2
}

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# The plist points at an absolute venv binary. Nothing here creates it, so on a
# clean clone launchd would spawn a path that does not exist and -- with
# KeepAlive true -- crash-loop silently while this script printed success.
HARNESSD="$REPO_DIR/.venv/bin/drover-harnessd"
[[ -x "$HARNESSD" ]] || {
  cat >&2 <<MSG
REFUSING: $HARNESSD is missing or not executable.

The launchd job runs that exact path, so installing now would crash-loop
silently. Create the venv first, then re-run this script:

  cd "$REPO_DIR" && uv sync
MSG
  exit 1
}

TOKEN_FILE="$HOME/.drover/api_token"
[[ -s "$TOKEN_FILE" ]] || { echo "put the fleet API token in $TOKEN_FILE first" >&2; exit 2; }
AUTH_VALUE="$(cat "$TOKEN_FILE")"

# Validate the token before installing anything (spec: fail loudly, never a silent retry loop).
# The `|| STATUS="000"` matters under `set -e`: if curl can't connect at all (bad host,
# DNS failure, timeout, TLS error) it exits non-zero and prints no http_code, which would
# otherwise kill the script right here with zero output instead of hitting the loud check below.
STATUS=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $AUTH_VALUE" "$CENTRAL_URL/harness/hosts") || STATUS="000"
[[ "$STATUS" == "200" ]] || { echo "token/URL check failed against $CENTRAL_URL (HTTP $STATUS)" >&2; exit 1; }

# A 200 *with* the token proves the token is right; it does not prove anything is
# being gated. If the hub's config has auth off, `tailscale funnel` publishes an
# unauthenticated /harness/* -- including /harness/relay and terminal attach -- to
# the whole internet. So also prove a bare request is refused.
BARE=$(curl -s -o /dev/null -w '%{http_code}' "$CENTRAL_URL/harness/hosts") || BARE="000"
[[ "$BARE" == "401" || "$BARE" == "403" ]] || {
  echo "REFUSING: $CENTRAL_URL answered HTTP $BARE with no token -- auth is not gating this hub" >&2
  echo "Enable auth on the hub before enrolling anything or exposing it via funnel." >&2
  exit 1
}

PLIST="$HOME/Library/LaunchAgents/com.drover.harnessd.plist"
mkdir -p "$HOME/Library/Logs/drover"

sed -e "s|__REPO_DIR__|$REPO_DIR|g" \
    -e "s|__HOST_ID__|$HOST_ID|g" \
    -e "s|__CENTRAL_URL__|$CENTRAL_URL|g" \
    -e "s|__HOME__|$HOME|g" \
    -e "s|__RELAY_FLAG__|--relay|g" \
    "$REPO_DIR/scripts/launchd/com.drover.harnessd-relay.plist.template" > "$PLIST"
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "enrolled $HOST_ID -> $CENTRAL_URL (relay); check the app for the new host"
