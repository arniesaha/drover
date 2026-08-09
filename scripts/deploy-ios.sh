#!/usr/bin/env bash
# Build, sign and install the Drover iOS app on a paired device.
#
# The README documents opening Xcode and pressing Run; this is the same thing
# without the GUI, which is what you want when the change you are deploying
# came out of a terminal in the first place.
#
#   scripts/deploy-ios.sh                 # build, install, launch
#   scripts/deploy-ios.sh --no-launch     # build and install only
#   scripts/deploy-ios.sh --device NAME   # pick a device by name substring
#
# Requires the device to be paired with Xcode and unlocked. Signing uses
# automatic provisioning against DROVER_TEAM_ID; the default is the team the
# app has always been signed with.
set -euo pipefail

TEAM_ID="${DROVER_TEAM_ID:-DK2PC4RH5G}"
BUNDLE_ID="com.arnab.drover"
DEVICE_MATCH="iPhone"
LAUNCH=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-launch) LAUNCH=0; shift ;;
    --device)    DEVICE_MATCH="$2"; shift 2 ;;
    -h|--help)   sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

cd "$(dirname "$0")/../apps/drover"

# The project file is gitignored and generated, so a fresh clone or a branch
# switch that added files needs this before the build sees them.
command -v xcodegen >/dev/null || { echo "xcodegen not found (brew install xcodegen)" >&2; exit 1; }
echo "==> regenerating project"
xcodegen generate >/dev/null

echo "==> finding device matching '${DEVICE_MATCH}'"
# The state column has to be matched as a whole field: "unavailable" contains
# "available", so a bare /available/ picks up offline devices and the run dies
# much later with an opaque CoreDevice 1011 instead of the message below.
# devicectl reports a usable device as either "available" or "connected"
# depending on how it is attached, so both count as reachable.
DEVICE_ID="$(xcrun devicectl list devices 2>/dev/null \
  | awk -v m="$DEVICE_MATCH" '$0 ~ m {
      online = 0
      for (i = 1; i <= NF; i++) if ($i == "available" || $i == "connected") online = 1
      if (!online) next
      for (i = 1; i <= NF; i++) if ($i ~ /^[0-9A-F]{8}-/) { print $i; exit }
    }')"
if [[ -z "${DEVICE_ID}" ]]; then
  echo "no available device matching '${DEVICE_MATCH}'. Paired devices:" >&2
  xcrun devicectl list devices >&2
  exit 1
fi
echo "    ${DEVICE_ID}"

echo "==> building (team ${TEAM_ID})"
xcodebuild \
  -project Drover.xcodeproj \
  -scheme Drover \
  -configuration Debug \
  -destination 'generic/platform=iOS' \
  -derivedDataPath .derivedData-device \
  DEVELOPMENT_TEAM="${TEAM_ID}" \
  CODE_SIGN_STYLE=Automatic \
  -allowProvisioningUpdates \
  build \
  | grep -E '^\*\* |error:|warning: no rule' || true

APP=".derivedData-device/Build/Products/Debug-iphoneos/Drover.app"
[[ -d "${APP}" ]] || { echo "build produced no app bundle at ${APP}" >&2; exit 1; }

echo "==> installing"
xcrun devicectl device install app --device "${DEVICE_ID}" "${APP}" | grep -E 'App installed|error' || true

if [[ "${LAUNCH}" == "1" ]]; then
  echo "==> launching"
  xcrun devicectl device process launch --device "${DEVICE_ID}" "${BUNDLE_ID}" \
    | grep -E 'Launched|error' || true
fi

echo "==> done"
