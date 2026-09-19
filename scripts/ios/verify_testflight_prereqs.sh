#!/usr/bin/env bash
# Optional operator checklist: local tools and workflow wiring presence.
# Does not read secrets, contact Apple, or prove Environment configuration.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

fail=0
note() { printf '%s\n' "$*"; }
ok() { printf 'OK  %s\n' "$*"; }
bad() { printf 'FAIL %s\n' "$*" >&2; fail=1; }

note "Internal TestFlight local prereq check (no secrets)."
note "Repository root: $ROOT"
note ""

require_cmd() {
  if command -v "$1" >/dev/null 2>&1; then
    ok "command $1"
  else
    bad "command $1 not found"
  fi
}

require_file() {
  if [[ -f "$1" ]]; then
    ok "file $1"
  else
    bad "missing file $1"
  fi
}

require_cmd python3
require_cmd bash

# CI uses Python 3.13; staging docs allow 3.11+.
if command -v python3 >/dev/null 2>&1; then
  py_ver="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
  major="${py_ver%%.*}"
  minor="${py_ver#*.}"
  if [[ "$major" -gt 3 ]] || { [[ "$major" -eq 3 && "$minor" -ge 11 ]]; }; then
    ok "python3 >= 3.11 ($py_ver)"
  else
    bad "python3 >= 3.11 required (found $py_ver)"
  fi
fi

# Optional local macOS tooling -- warn only when absent (Linux operators OK).
for optional in xcodegen xcodebuild xcrun security plutil gh; do
  if command -v "$optional" >/dev/null 2>&1; then
    ok "optional command $optional"
  else
    note "SKIP optional command $optional (not required for CI dispatch)"
  fi
done

require_file ".github/workflows/ios-testflight-internal.yml"
require_file "apps/drover/docs/internal-testflight-runbook.md"
require_file "apps/drover/docs/distribution.md"
require_file "deploy/testflight-staging/README.md"
require_file "scripts/testflight/verify_staging.py"
require_file "scripts/testflight/stage.py"
require_file "scripts/ios/setup_distribution_signing.sh"
require_file "scripts/ios/archive.sh"
require_file "scripts/ios/export_ipa.sh"
require_file "scripts/ios/upload_testflight.sh"
require_file "scripts/ios/cleanup_distribution_signing.sh"

note ""
note "Checking workflow Environment and secret/variable names…"

workflow=".github/workflows/ios-testflight-internal.yml"
expect_env=(
  "environment: ios-testflight-staging"
  "environment: ios-testflight-upload"
)
expect_refs=(
  "vars.DROVER_TESTFLIGHT_STAGING_URL"
  "secrets.DROVER_TESTFLIGHT_PREFLIGHT_TOKEN"
  "secrets.DROVER_DISTRIBUTION_P12_BASE64"
  "secrets.DROVER_DISTRIBUTION_P12_PASSWORD"
  "secrets.DROVER_DISTRIBUTION_PROFILE_BASE64"
  "secrets.DROVER_DISTRIBUTION_TEAM_ID"
  "secrets.DROVER_DISTRIBUTION_PROFILE_UUID"
  "secrets.DROVER_DISTRIBUTION_IDENTITY_SHA1"
  "secrets.DROVER_DISTRIBUTION_IDENTITY_NAME"
  "secrets.DROVER_APPSTORE_API_KEY_ID"
  "secrets.DROVER_APPSTORE_API_ISSUER_ID"
  "secrets.DROVER_APPSTORE_API_PRIVATE_KEY_BASE64"
)

for needle in "${expect_env[@]}" "${expect_refs[@]}"; do
  if grep -Fq "$needle" "$workflow"; then
    ok "workflow contains $needle"
  else
    bad "workflow missing $needle"
  fi
done

if grep -Fq "DROVER_ASC_KEY_ID" "$workflow"; then
  bad "workflow unexpectedly references DROVER_ASC_KEY_ID (local placeholder only)"
else
  ok "workflow does not use DROVER_ASC_* local placeholders as secret names"
fi

note ""
if [[ "$fail" -ne 0 ]]; then
  note "Prereq check failed. See apps/drover/docs/internal-testflight-runbook.md"
  exit 1
fi
note "Local wiring looks consistent. Still required before a live run:"
note "  - GitHub Environments ios-testflight-staging / ios-testflight-upload"
note "  - Repository variable DROVER_TESTFLIGHT_STAGING_URL"
note "  - Environment secrets listed in the runbook"
note "  - Mac Mini staging prepare/activate/probe for the candidate SHA"
note "  - App Store Connect app record + processing after upload"
exit 0
