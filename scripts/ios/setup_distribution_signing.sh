#!/usr/bin/env bash
# Materialize a protected distribution identity and profile without logging secrets.
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/ios/setup_distribution_signing.sh --workspace DIRECTORY --github-output FILE

Reads protected DROVER_DISTRIBUTION_* environment values and writes private
signing references to DIRECTORY. The caller must invoke cleanup_distribution_signing.sh.
USAGE
}

fail() {
  printf '%s\n' "$1" >&2
  exit 1
}

require_environment() {
  local name="$1"
  local value="${!name:-}"
  [[ -n "$value" ]] || fail "required protected signing input is unavailable"
  printf '%s' "$value"
}

is_safe_xcconfig_path() {
  local path="$1"
  [[ "$path" = /* && "$path" != *$'\n'* && "$path" != *$'\r'* \
    && "$path" != *\\* && "$path" != *'$'* && "$path" != *'"'* ]]
}

WORKSPACE=""
GITHUB_OUTPUT_FILE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --workspace)
      [[ $# -ge 2 ]] || fail "--workspace requires a value"
      WORKSPACE="$2"
      shift 2
      ;;
    --github-output)
      [[ $# -ge 2 ]] || fail "--github-output requires a value"
      GITHUB_OUTPUT_FILE="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      fail "unknown option"
      ;;
  esac
done

[[ "$WORKSPACE" = /* ]] || fail "workspace must be an absolute path"
is_safe_xcconfig_path "$WORKSPACE" \
  || fail "workspace path contains unsupported characters"
[[ -n "$GITHUB_OUTPUT_FILE" ]] || fail "--github-output is required"
[[ ! -e "$WORKSPACE" && ! -L "$WORKSPACE" ]] || fail "workspace already exists"

P12_BASE64="$(require_environment DROVER_DISTRIBUTION_P12_BASE64)"
P12_PASSWORD="$(require_environment DROVER_DISTRIBUTION_P12_PASSWORD)"
PROFILE_BASE64="$(require_environment DROVER_DISTRIBUTION_PROFILE_BASE64)"
TEAM_ID="$(require_environment DROVER_DISTRIBUTION_TEAM_ID)"
PROFILE_UUID="$(require_environment DROVER_DISTRIBUTION_PROFILE_UUID)"
IDENTITY_SHA1="$(require_environment DROVER_DISTRIBUTION_IDENTITY_SHA1)"
IDENTITY_NAME="$(require_environment DROVER_DISTRIBUTION_IDENTITY_NAME)"

[[ "$TEAM_ID" =~ ^[A-Z0-9]{10}$ ]] || fail "distribution team reference is invalid"
[[ "$PROFILE_UUID" =~ ^[[:xdigit:]]{8}-[[:xdigit:]]{4}-[[:xdigit:]]{4}-[[:xdigit:]]{4}-[[:xdigit:]]{12}$ ]] \
  || fail "distribution profile reference is invalid"
[[ "$IDENTITY_SHA1" =~ ^[[:xdigit:]]{40}$ ]] \
  || fail "distribution identity reference is invalid"
[[ "$IDENTITY_NAME" == "Apple Distribution: "* && "$IDENTITY_NAME" == *"($TEAM_ID)" \
  && "$IDENTITY_NAME" != *$'\n'* && "$IDENTITY_NAME" != *$'\r'* ]] \
  || fail "distribution identity name is invalid"

for command in base64 grep openssl plutil python3 security swift; do
  command -v "$command" >/dev/null 2>&1 || fail "required signing setup command is unavailable"
done

umask 077
mkdir "$WORKSPACE"
KEYCHAIN_PATH="$HOME/Library/Keychains/drover-distribution-$PROFILE_UUID.keychain-db"
P12_PATH="$WORKSPACE/distribution.p12"
PROFILE_PATH="$WORKSPACE/distribution.mobileprovision"
PROFILE_PLIST="$WORKSPACE/distribution-profile.plist"
SIGNING_CONFIG="$WORKSPACE/signing.xcconfig"
STATE_FILE="$WORKSPACE/signing-state"
KEYCHAIN_STATE="$WORKSPACE/keychain-state.json"
IMPORT_LOG="$WORKSPACE/import.log"
IDENTITY_CHECK_LOG="$WORKSPACE/identity-check.log"
PARTITION_LIST_LOG="$WORKSPACE/partition-list.log"
PROFILE_DESTINATION="$HOME/Library/MobileDevice/Provisioning Profiles/$PROFILE_UUID.mobileprovision"
PROFILE_INSTALLED=false

restore_keychain_state() {
  [[ -f "$KEYCHAIN_STATE" && ! -L "$KEYCHAIN_STATE" ]] || return 0
  python3 - "$KEYCHAIN_STATE" <<'PY' >/dev/null 2>&1
import json
import pathlib
import subprocess
import sys

state = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
subprocess.run(
    ["security", "default-keychain", "-d", "user", "-s", state["default"]],
    check=True,
)
subprocess.run(
    ["security", "list-keychains", "-d", "user", "-s", *state["search"]],
    check=True,
)
PY
}

cleanup_failure() {
  local result="$?"
  if [[ "$result" -ne 0 ]]; then
    local cleanup_incomplete=false
    if ! security delete-keychain "$KEYCHAIN_PATH" >/dev/null 2>&1; then
      rm -f "$KEYCHAIN_PATH" >/dev/null 2>&1 || true
    fi
    if [[ -e "$KEYCHAIN_PATH" || -L "$KEYCHAIN_PATH" ]]; then
      cleanup_incomplete=true
    fi
    restore_keychain_state || cleanup_incomplete=true
    if [[ "$PROFILE_INSTALLED" = true ]]; then
      rm -f "$PROFILE_DESTINATION"
    fi
    if [[ "$cleanup_incomplete" = true ]]; then
      printf '%s\n' \
        "keychain=$KEYCHAIN_PATH" \
        "keychain_state=$KEYCHAIN_STATE" \
        > "$WORKSPACE/cleanup-recovery"
      rm -f "$P12_PATH" "$PROFILE_PATH" "$PROFILE_PLIST" "$SIGNING_CONFIG" \
        "$STATE_FILE" "$IMPORT_LOG" "$IDENTITY_CHECK_LOG" \
        "$PARTITION_LIST_LOG" "$WORKSPACE/keychain-settings.log" \
        "$WORKSPACE/keychain-discovery.log" "$WORKSPACE/p12-decode.log" \
        "$WORKSPACE/profile-decode.log" "$WORKSPACE/profile-cms.log" \
        "$WORKSPACE/profile-uuid.log" "$WORKSPACE/keychain-password.log"
      printf '%s\n' "distribution signing cleanup is incomplete; recovery state retained" >&2
    else
      rm -rf "$WORKSPACE"
    fi
  fi
  exit "$result"
}
trap cleanup_failure EXIT

printf '%s' "$P12_BASE64" | base64 -D > "$P12_PATH" 2>"$WORKSPACE/p12-decode.log" \
  || fail "distribution identity material could not be decoded"
printf '%s' "$PROFILE_BASE64" | base64 -D > "$PROFILE_PATH" 2>"$WORKSPACE/profile-decode.log" \
  || fail "distribution profile could not be decoded"
security cms -D -i "$PROFILE_PATH" > "$PROFILE_PLIST" 2>"$WORKSPACE/profile-cms.log" \
  || fail "distribution profile could not be decoded"
ACTUAL_PROFILE_UUID="$(plutil -extract UUID raw "$PROFILE_PLIST" 2>"$WORKSPACE/profile-uuid.log")" \
  || fail "distribution profile could not be read"
[[ "$ACTUAL_PROFILE_UUID" = "$PROFILE_UUID" ]] \
  || fail "distribution profile does not match the approved reference"

mkdir -p "$(dirname "$PROFILE_DESTINATION")"
[[ ! -e "$PROFILE_DESTINATION" && ! -L "$PROFILE_DESTINATION" ]] \
  || fail "approved distribution profile already exists"
cp "$PROFILE_PATH" "$PROFILE_DESTINATION"
PROFILE_INSTALLED=true

KEYCHAIN_PASSWORD="$(openssl rand -base64 32 2>"$WORKSPACE/keychain-password.log")" \
  || fail "temporary signing keychain could not be created"
mkdir -p "$(dirname "$KEYCHAIN_PATH")"
is_safe_xcconfig_path "$KEYCHAIN_PATH" \
  || fail "temporary signing keychain path contains unsupported characters"
[[ ! -e "$KEYCHAIN_PATH" && ! -L "$KEYCHAIN_PATH" ]] \
  || fail "temporary signing keychain already exists"
DROVER_SIGNING_KEYCHAIN_PASSWORD="$KEYCHAIN_PASSWORD" \
DROVER_SIGNING_P12_PASSWORD="$P12_PASSWORD" \
swift "$(dirname "$0")/import_distribution_identity.swift" \
  --p12-path "$P12_PATH" --keychain-path "$KEYCHAIN_PATH" \
  > "$IMPORT_LOG" 2>&1 || fail "distribution identity import failed"
security set-keychain-settings -lut 21600 "$KEYCHAIN_PATH" \
  >"$WORKSPACE/keychain-settings.log" 2>&1 \
  || fail "temporary signing keychain could not be configured"
python3 - "$KEYCHAIN_STATE" "$KEYCHAIN_PATH" "$IDENTITY_SHA1" <<'PY' \
  >"$WORKSPACE/keychain-discovery.log" 2>&1 \
  || fail "temporary signing keychain could not be isolated"
import json
import pathlib
import shlex
import subprocess
import sys

state_path, keychain, identity_sha = sys.argv[1:]
search_result = subprocess.run(
    ["security", "list-keychains", "-d", "user"],
    capture_output=True,
    text=True,
    check=True,
)
default_result = subprocess.run(
    ["security", "default-keychain", "-d", "user"],
    capture_output=True,
    text=True,
    check=True,
)
search = shlex.split(search_result.stdout)
defaults = shlex.split(default_result.stdout)
if len(defaults) != 1:
    raise SystemExit(1)
path = pathlib.Path(state_path)
with path.open("x", encoding="utf-8") as output:
    json.dump({"default": defaults[0], "search": search}, output)
path.chmod(0o600)

active = []
for candidate in search:
    candidate_path = pathlib.Path(candidate)
    if not candidate_path.is_file() or candidate_path.is_symlink():
        continue
    result = subprocess.run(
        ["security", "find-identity", "-v", "-p", "codesigning", candidate],
        capture_output=True,
        text=True,
    )
    if identity_sha not in result.stdout:
        active.append(candidate)
subprocess.run(
    ["security", "list-keychains", "-d", "user", "-s", *active, keychain],
    capture_output=True,
    check=True,
)
subprocess.run(
    ["security", "default-keychain", "-d", "user", "-s", keychain],
    capture_output=True,
    check=True,
)
PY
security set-key-partition-list -S apple: -l "Imported Private Key" \
  -k "$KEYCHAIN_PASSWORD" "$KEYCHAIN_PATH" \
  > "$PARTITION_LIST_LOG" 2>&1 \
  || fail "distribution identity authorization failed"

if ! security find-identity -v -p codesigning "$KEYCHAIN_PATH" \
  >"$IDENTITY_CHECK_LOG" 2>&1; then
  fail "approved distribution identity is unavailable"
fi
if ! grep -Fq "$IDENTITY_SHA1 \"$IDENTITY_NAME\"" "$IDENTITY_CHECK_LOG"; then
  fail "approved distribution identity is unavailable"
fi

printf '%s\n' \
  "DROVER_CODE_SIGN_STYLE = Manual" \
  "DROVER_DEVELOPMENT_TEAM = $TEAM_ID" \
  "DROVER_CODE_SIGN_IDENTITY = $IDENTITY_NAME" \
  "DROVER_PROVISIONING_PROFILE_SPECIFIER = $PROFILE_UUID" \
  "DROVER_OTHER_CODE_SIGN_FLAGS = --keychain \"$KEYCHAIN_PATH\"" \
  > "$SIGNING_CONFIG"
printf '%s\n' \
  "workspace=$WORKSPACE" \
  "keychain=$KEYCHAIN_PATH" \
  "profile=$PROFILE_DESTINATION" \
  "signing_config=$SIGNING_CONFIG" \
  "keychain_state=$KEYCHAIN_STATE" \
  > "$STATE_FILE"
rm -f "$P12_PATH" "$PROFILE_PATH" "$PROFILE_PLIST" "$IMPORT_LOG" \
  "$IDENTITY_CHECK_LOG" "$PARTITION_LIST_LOG" \
  "$WORKSPACE/keychain-settings.log" "$WORKSPACE/keychain-discovery.log" \
  "$WORKSPACE/p12-decode.log" "$WORKSPACE/profile-decode.log" \
  "$WORKSPACE/profile-cms.log" "$WORKSPACE/profile-uuid.log" \
  "$WORKSPACE/keychain-password.log"

printf 'signing_config=%s\nkeychain_path=%s\nprofile_path=%s\nstate_file=%s\n' \
  "$SIGNING_CONFIG" "$KEYCHAIN_PATH" "$PROFILE_DESTINATION" "$STATE_FILE" \
  >> "$GITHUB_OUTPUT_FILE"
trap - EXIT
printf '%s\n' "distribution signing setup complete"
