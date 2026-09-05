#!/usr/bin/env bash
# Archive and validate a candidate without selecting a signing identity.
set -euo pipefail

readonly ARTIFACT_ROOT="${DROVER_IOS_ARTIFACT_ROOT:-/Volumes/M2 1}"
readonly REQUIRED_IOS_SDK="26.0"

usage() {
  cat <<'USAGE'
Usage: scripts/ios/archive.sh --version VERSION --build BUILD --output DIRECTORY

Creates DIRECTORY/Drover.xcarchive, an archive zip and an archive-record.json.
DIRECTORY must not already exist and must be under the selected artifact root.
The default artifact root is /Volumes/M2 1.
USAGE
}

fail() {
  printf '%s\n' "$1" >&2
  exit 1
}

version_at_least() {
  local actual="$1"
  local required="$2"
  local actual_major actual_minor actual_patch required_major required_minor required_patch
  IFS=. read -r actual_major actual_minor actual_patch <<<"$actual"
  IFS=. read -r required_major required_minor required_patch <<<"$required"
  actual_minor="${actual_minor:-0}"
  actual_patch="${actual_patch:-0}"
  required_minor="${required_minor:-0}"
  required_patch="${required_patch:-0}"

  if (( actual_major != required_major )); then
    (( actual_major > required_major ))
    return
  fi
  if (( actual_minor != required_minor )); then
    (( actual_minor > required_minor ))
    return
  fi
  (( actual_patch >= required_patch ))
}

VERSION=""
BUILD=""
OUTPUT=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version)
      [[ $# -ge 2 ]] || fail "--version requires a value"
      VERSION="$2"
      shift 2
      ;;
    --build)
      [[ $# -ge 2 ]] || fail "--build requires a value"
      BUILD="$2"
      shift 2
      ;;
    --output)
      [[ $# -ge 2 ]] || fail "--output requires a value"
      OUTPUT="$2"
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

[[ -n "$VERSION" ]] || fail "--version is required"
[[ -n "$BUILD" ]] || fail "--build is required"
[[ -n "$OUTPUT" ]] || fail "--output is required"
[[ "$VERSION" =~ ^[0-9]+(\.[0-9]+){0,2}$ ]] \
  || fail "version must be an expanded numeric version"
[[ "$BUILD" =~ ^[0-9]+(\.[0-9]+){0,2}$ ]] \
  || fail "build must be an expanded numeric build number"
[[ "$OUTPUT" = /* ]] || fail "output must be an absolute path"
[[ ! -e "$OUTPUT" && ! -L "$OUTPUT" ]] || fail "output directory already exists"

OUTPUT_PARENT="$(dirname "$OUTPUT")"
[[ -d "$OUTPUT_PARENT" ]] || fail "output parent directory does not exist"
OUTPUT_PARENT_REAL="$(cd "$OUTPUT_PARENT" && pwd -P)"
case "$OUTPUT_PARENT_REAL" in
  "$ARTIFACT_ROOT"|"$ARTIFACT_ROOT"/*) ;;
  *) fail "output must be under $ARTIFACT_ROOT" ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPOSITORY_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd -P)"
APP_DIRECTORY="$REPOSITORY_ROOT/apps/drover"
VERIFY_SCRIPT="$SCRIPT_DIR/verify_archive.py"

for command in xcodebuild xcodegen xcrun xcode-select git ditto shasum python3; do
  command -v "$command" >/dev/null 2>&1 || fail "required command is unavailable"
done
[[ -f "$VERIFY_SCRIPT" ]] || fail "distribution verifier is unavailable"

XCODE_VERSION="$(xcodebuild -version | awk '$1 == "Xcode" { print $2; exit }')"
[[ "$XCODE_VERSION" =~ ^[0-9]+(\.[0-9]+){0,2}$ ]] \
  || fail "selected Xcode version could not be determined"
XCODE_MAJOR="${XCODE_VERSION%%.*}"
(( XCODE_MAJOR >= 26 )) || fail "selected Xcode does not meet the Xcode 26 requirement"

IPHONEOS_SDK="$(xcrun --sdk iphoneos --show-sdk-version)"
[[ "$IPHONEOS_SDK" =~ ^[0-9]+(\.[0-9]+){0,2}$ ]] \
  || fail "selected iPhoneOS SDK version could not be determined"
version_at_least "$IPHONEOS_SDK" "$REQUIRED_IOS_SDK" \
  || fail "selected iPhoneOS SDK is below $REQUIRED_IOS_SDK"
SELECTED_DEVELOPER_DIR="$(xcode-select -p)"

COMMIT="$(git -C "$REPOSITORY_ROOT" rev-parse HEAD)"
if [[ -z "$(git -C "$REPOSITORY_ROOT" status --porcelain --untracked-files=normal)" ]]; then
  CLEAN_TREE=true
else
  CLEAN_TREE=false
fi

mkdir "$OUTPUT"
ARCHIVE_PATH="$OUTPUT/Drover.xcarchive"
ARCHIVE_ZIP="$OUTPUT/Drover.xcarchive.zip"
RECORD_PATH="$OUTPUT/archive-record.json"
LOG_DIRECTORY="$(mktemp -d "$OUTPUT_PARENT_REAL/.drover-ios-archive.XXXXXX")"
trap 'rm -rf "$LOG_DIRECTORY"' EXIT

if ! (cd "$APP_DIRECTORY" && xcodegen generate) >"$LOG_DIRECTORY/xcodegen.log" 2>&1; then
  fail "project generation failed"
fi

if ! xcodebuild \
  -project "$APP_DIRECTORY/Drover.xcodeproj" \
  -scheme DroverAppStore \
  -configuration StoreRelease \
  -sdk iphoneos \
  -destination 'generic/platform=iOS' \
  -archivePath "$ARCHIVE_PATH" \
  "MARKETING_VERSION=$VERSION" \
  "CURRENT_PROJECT_VERSION=$BUILD" \
  archive >"$LOG_DIRECTORY/archive.log" 2>&1; then
  fail "archive failed; no signing details were printed"
fi

if ! python3 "$VERIFY_SCRIPT" \
  --app "$ARCHIVE_PATH" \
  --expected-version "$VERSION" \
  --expected-build "$BUILD" \
  --minimum-ios-sdk "$REQUIRED_IOS_SDK" >"$LOG_DIRECTORY/verify.log" 2>&1; then
  fail "archive verification failed; signed artifact was rejected"
fi

if ! ditto -c -k --keepParent "$ARCHIVE_PATH" "$ARCHIVE_ZIP" \
  >"$LOG_DIRECTORY/archive-zip.log" 2>&1; then
  fail "archive packaging failed"
fi
ARTIFACT_SHA256="$(shasum -a 256 "$ARCHIVE_ZIP" | awk '{ print $1 }')"
[[ "$ARTIFACT_SHA256" =~ ^[0-9a-f]{64}$ ]] || fail "archive hash could not be determined"

python3 - "$RECORD_PATH" "$COMMIT" "$CLEAN_TREE" "$XCODE_VERSION" \
  "$SELECTED_DEVELOPER_DIR" "$IPHONEOS_SDK" "$VERSION" "$BUILD" "$ARCHIVE_PATH" \
  "$ARCHIVE_ZIP" "$ARTIFACT_SHA256" <<'PY'
import json
import pathlib
import sys

(
    record_path,
    commit,
    clean_tree,
    xcode_version,
    developer_dir,
    iphoneos_sdk,
    version,
    build,
    archive_path,
    archive_zip,
    artifact_sha256,
) = sys.argv[1:]
pathlib.Path(record_path).write_text(
    json.dumps(
        {
            "archive": archive_path,
            "archive_zip": archive_zip,
            "archive_zip_sha256": artifact_sha256,
            "build": build,
            "clean_tree": clean_tree == "true",
            "commit": commit,
            "iphoneos_sdk": iphoneos_sdk,
            "selected_developer_dir": developer_dir,
            "version": version,
            "xcode_version": xcode_version,
        },
        indent=2,
        sort_keys=True,
    )
    + "\n",
    encoding="utf-8",
)
PY

printf 'archive created: version=%s build=%s xcode=%s sdk=%s sha256=%s\n' \
  "$VERSION" "$BUILD" "$XCODE_VERSION" "$IPHONEOS_SDK" "$ARTIFACT_SHA256"
