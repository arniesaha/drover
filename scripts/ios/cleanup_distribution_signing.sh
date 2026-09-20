#!/usr/bin/env bash
# Remove only the temporary keychain, profile, and configuration recorded by setup.
set -euo pipefail

fail() {
  printf '%s\n' "$1" >&2
  exit 1
}

STATE_FILE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --state)
      [[ $# -ge 2 ]] || fail "--state requires a value"
      STATE_FILE="$2"
      shift 2
      ;;
    *) fail "unknown option" ;;
  esac
done

[[ "$STATE_FILE" = /* && -f "$STATE_FILE" && ! -L "$STATE_FILE" ]] \
  || fail "state file is unavailable"

WORKSPACE=""
KEYCHAIN_PATH=""
PROFILE_PATH=""
SIGNING_CONFIG=""
KEYCHAIN_STATE=""
while IFS='=' read -r key value; do
  case "$key" in
    workspace) WORKSPACE="$value" ;;
    keychain) KEYCHAIN_PATH="$value" ;;
    profile) PROFILE_PATH="$value" ;;
    signing_config) SIGNING_CONFIG="$value" ;;
    keychain_state) KEYCHAIN_STATE="$value" ;;
    *) fail "state file is invalid" ;;
  esac
done < "$STATE_FILE"

[[ "$WORKSPACE" = /* && -d "$WORKSPACE" && ! -L "$WORKSPACE" ]] \
  || fail "recorded workspace is unavailable"
[[ "$STATE_FILE" = "$WORKSPACE/signing-state" ]] \
  || fail "state file is outside the recorded workspace"
[[ "$SIGNING_CONFIG" = "$WORKSPACE/signing.xcconfig" ]] \
  || fail "recorded signing configuration is invalid"
[[ "$KEYCHAIN_STATE" = "$WORKSPACE/keychain-state.json" \
  && -f "$KEYCHAIN_STATE" && ! -L "$KEYCHAIN_STATE" ]] \
  || fail "recorded keychain state is invalid"
[[ "$PROFILE_PATH" == */Library/MobileDevice/Provisioning\ Profiles/*.mobileprovision ]] \
  || fail "recorded provisioning profile path is invalid"
PROFILE_UUID="${PROFILE_PATH##*/}"
PROFILE_UUID="${PROFILE_UUID%.mobileprovision}"
[[ "$PROFILE_UUID" =~ ^[[:xdigit:]]{8}-[[:xdigit:]]{4}-[[:xdigit:]]{4}-[[:xdigit:]]{4}-[[:xdigit:]]{12}$ ]] \
  || fail "recorded provisioning profile path is invalid"
[[ "$KEYCHAIN_PATH" = "$HOME/Library/Keychains/drover-distribution-$PROFILE_UUID.keychain-db" ]] \
  || fail "recorded keychain path is invalid"

if ! security delete-keychain "$KEYCHAIN_PATH" >/dev/null 2>&1; then
  rm -f "$KEYCHAIN_PATH" >/dev/null 2>&1 || true
fi
KEYCHAIN_REMOVED=true
if [[ -e "$KEYCHAIN_PATH" || -L "$KEYCHAIN_PATH" ]]; then
  KEYCHAIN_REMOVED=false
fi

KEYCHAIN_RESTORED=true
python3 - "$KEYCHAIN_STATE" <<'PY' \
  || KEYCHAIN_RESTORED=false
import json
import pathlib
import subprocess
import sys

state = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
default = state.get("default")
search = state.get("search")
if not isinstance(default, str) or not default.startswith("/"):
    raise SystemExit(1)
if not isinstance(search, list) or not all(
    isinstance(item, str) and item.startswith("/") for item in search
):
    raise SystemExit(1)
subprocess.run(
    ["security", "default-keychain", "-d", "user", "-s", default],
    capture_output=True,
    check=True,
)
subprocess.run(
    ["security", "list-keychains", "-d", "user", "-s", *search],
    capture_output=True,
    check=True,
)
PY
rm -f "$PROFILE_PATH"
if [[ "$KEYCHAIN_REMOVED" = true && "$KEYCHAIN_RESTORED" = true ]]; then
  rm -rf "$WORKSPACE"
  printf '%s\n' "temporary distribution signing material removed"
  exit 0
fi
[[ "$KEYCHAIN_REMOVED" = true ]] \
  || printf '%s\n' "temporary distribution signing keychain could not be removed" >&2
[[ "$KEYCHAIN_RESTORED" = true ]] \
  || printf '%s\n' "temporary distribution signing state could not be restored" >&2
exit 1
