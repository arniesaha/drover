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

for command in base64 grep openssl plutil security swift; do
  command -v "$command" >/dev/null 2>&1 || fail "required signing setup command is unavailable"
done

umask 077
mkdir "$WORKSPACE"
KEYCHAIN_PATH="$WORKSPACE/distribution.keychain-db"
P12_PATH="$WORKSPACE/distribution.p12"
PROFILE_PATH="$WORKSPACE/distribution.mobileprovision"
PROFILE_PLIST="$WORKSPACE/distribution-profile.plist"
SIGNING_CONFIG="$WORKSPACE/signing.xcconfig"
STATE_FILE="$WORKSPACE/signing-state"
IMPORT_LOG="$WORKSPACE/import.log"
IDENTITY_CHECK_LOG="$WORKSPACE/identity-check.log"
PROFILE_DESTINATION="$HOME/Library/MobileDevice/Provisioning Profiles/$PROFILE_UUID.mobileprovision"
PROFILE_INSTALLED=false

cleanup_failure() {
  local result="$?"
  if [[ "$result" -ne 0 ]]; then
    security delete-keychain "$KEYCHAIN_PATH" >/dev/null 2>&1 || true
    if [[ "$PROFILE_INSTALLED" = true ]]; then
      rm -f "$PROFILE_DESTINATION"
    fi
    rm -rf "$WORKSPACE"
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
DROVER_SIGNING_KEYCHAIN_PASSWORD="$KEYCHAIN_PASSWORD" \
DROVER_SIGNING_P12_PASSWORD="$P12_PASSWORD" \
swift "$(dirname "$0")/import_distribution_identity.swift" \
  --p12-path "$P12_PATH" --keychain-path "$KEYCHAIN_PATH" \
  > "$IMPORT_LOG" 2>&1 || fail "distribution identity import failed"

if ! security find-identity -v -p codesigning "$KEYCHAIN_PATH" \
  >"$IDENTITY_CHECK_LOG" 2>&1; then
  fail "approved distribution identity is unavailable"
fi
if ! grep -Fq "$IDENTITY_SHA1 \"$IDENTITY_NAME\"" "$IDENTITY_CHECK_LOG"; then
  fail "approved distribution identity is unavailable"
fi

printf '%s\n' \
  "CODE_SIGN_STYLE = Manual" \
  "DEVELOPMENT_TEAM = $TEAM_ID" \
  "CODE_SIGN_IDENTITY = $IDENTITY_NAME" \
  "PROVISIONING_PROFILE_SPECIFIER = $PROFILE_UUID" \
  "OTHER_CODE_SIGN_FLAGS = --keychain \"$KEYCHAIN_PATH\"" \
  > "$SIGNING_CONFIG"
printf '%s\n' \
  "workspace=$WORKSPACE" \
  "keychain=$KEYCHAIN_PATH" \
  "profile=$PROFILE_DESTINATION" \
  "signing_config=$SIGNING_CONFIG" \
  > "$STATE_FILE"
rm -f "$P12_PATH" "$PROFILE_PATH" "$PROFILE_PLIST" "$IMPORT_LOG" \
  "$IDENTITY_CHECK_LOG" \
  "$WORKSPACE/p12-decode.log" "$WORKSPACE/profile-decode.log" \
  "$WORKSPACE/profile-cms.log" "$WORKSPACE/profile-uuid.log" \
  "$WORKSPACE/keychain-password.log"

printf 'signing_config=%s\nkeychain_path=%s\nprofile_path=%s\nstate_file=%s\n' \
  "$SIGNING_CONFIG" "$KEYCHAIN_PATH" "$PROFILE_DESTINATION" "$STATE_FILE" \
  >> "$GITHUB_OUTPUT_FILE"
trap - EXIT
printf '%s\n' "distribution signing setup complete"
