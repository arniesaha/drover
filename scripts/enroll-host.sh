#!/usr/bin/env bash
# Enroll this machine as a Drover harness host.
# Usage: ./scripts/enroll-host.sh --host-id work-laptop --central-url https://mini.tailnet.ts.net [--relay]
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

TOKEN_FILE="$HOME/.drover/api_token"
[[ -s "$TOKEN_FILE" ]] || { echo "put the fleet API token in $TOKEN_FILE first" >&2; exit 2; }
TOKEN="$(cat "$TOKEN_FILE")"

# Validate the token before installing anything (spec: fail loudly, never a silent retry loop).
# The `|| STATUS="000"` matters under `set -e`: if curl can't connect at all (bad host,
# DNS failure, timeout, TLS error) it exits non-zero and prints no http_code, which would
# otherwise kill the script right here with zero output instead of hitting the loud check below.
STATUS=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $TOKEN" "$CENTRAL_URL/harness/hosts") || STATUS="000"
[[ "$STATUS" == "200" ]] || { echo "token/URL check failed against $CENTRAL_URL (HTTP $STATUS)" >&2; exit 1; }

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RELAY_FLAG=""
[[ "$RELAY" == 1 ]] && RELAY_FLAG="--relay"

PLIST="$HOME/Library/LaunchAgents/com.drover.harnessd.plist"
mkdir -p "$HOME/Library/Logs/drover"

# Each plist ProgramArguments entry is a fixed argv slot (unlike a shell command
# line, an empty <string> does NOT disappear via word-splitting), so when --relay
# wasn't requested we delete the __RELAY_FLAG__ line outright instead of blanking it.
RELAY_LINE_EDIT=(-e "s|__RELAY_FLAG__|$RELAY_FLAG|g")
[[ -z "$RELAY_FLAG" ]] && RELAY_LINE_EDIT=(-e "/__RELAY_FLAG__/d")

sed -e "s|__REPO_DIR__|$REPO_DIR|g" \
    -e "s|__HOST_ID__|$HOST_ID|g" \
    -e "s|__CENTRAL_URL__|$CENTRAL_URL|g" \
    -e "s|__HOME__|$HOME|g" \
    "${RELAY_LINE_EDIT[@]}" \
    "$REPO_DIR/scripts/launchd/com.drover.harnessd-relay.plist.template" > "$PLIST"
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "enrolled $HOST_ID -> $CENTRAL_URL (relay=$RELAY); check the app for the new host"
