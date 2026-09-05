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
while IFS='=' read -r key value; do
  case "$key" in
    workspace) WORKSPACE="$value" ;;
    keychain) KEYCHAIN_PATH="$value" ;;
    profile) PROFILE_PATH="$value" ;;
    signing_config) SIGNING_CONFIG="$value" ;;
    *) fail "state file is invalid" ;;
  esac
done < "$STATE_FILE"

[[ "$WORKSPACE" = /* && -d "$WORKSPACE" && ! -L "$WORKSPACE" ]] \
  || fail "recorded workspace is unavailable"
[[ "$STATE_FILE" = "$WORKSPACE/signing-state" ]] \
  || fail "state file is outside the recorded workspace"
[[ "$KEYCHAIN_PATH" = "$WORKSPACE/distribution.keychain-db" ]] \
  || fail "recorded keychain path is invalid"
[[ "$SIGNING_CONFIG" = "$WORKSPACE/signing.xcconfig" ]] \
  || fail "recorded signing configuration is invalid"
[[ "$PROFILE_PATH" == */Library/MobileDevice/Provisioning\ Profiles/*.mobileprovision ]] \
  || fail "recorded provisioning profile path is invalid"

security delete-keychain "$KEYCHAIN_PATH" >/dev/null 2>&1 || true
rm -f "$PROFILE_PATH"
rm -rf "$WORKSPACE"
printf '%s\n' "temporary distribution signing material removed"
