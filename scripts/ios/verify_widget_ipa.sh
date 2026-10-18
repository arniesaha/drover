#!/usr/bin/env bash
# Verify the exported WidgetKit extension without retaining the signed IPA contents.
set +x
set -euo pipefail
umask 077

fail() {
  printf '%s\n' "$1" >&2
  exit 1
}

IPA=""
EXPECTED_PROFILE_UUID=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --ipa)
      [[ $# -ge 2 ]] || fail "--ipa requires a value"
      IPA="$2"
      shift 2
      ;;
    --expected-profile-uuid)
      [[ $# -ge 2 ]] || fail "--expected-profile-uuid requires a value"
      EXPECTED_PROFILE_UUID="$2"
      shift 2
      ;;
    *) fail "unknown option" ;;
  esac
done

[[ "$IPA" = /* && -f "$IPA" && ! -L "$IPA" ]] || fail "exported IPA is unavailable"
[[ "$EXPECTED_PROFILE_UUID" =~ ^[[:xdigit:]]{8}-[[:xdigit:]]{4}-[[:xdigit:]]{4}-[[:xdigit:]]{4}-[[:xdigit:]]{12}$ ]] \
  || fail "expected widget profile reference is invalid"
for command in ditto codesign security python3; do
  command -v "$command" >/dev/null 2>&1 || fail "required widget verification command is unavailable"
done

SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT
ditto -x -k "$IPA" "$SCRATCH/unpacked" >/dev/null 2>&1 \
  || fail "exported IPA could not be unpacked"
APP="$SCRATCH/unpacked/Payload/Drover.app"
WIDGET="$APP/PlugIns/DroverWidget.appex"
[[ -d "$WIDGET" && ! -L "$WIDGET" ]] || fail "exported IPA is missing DroverWidget.appex"

codesign -d --entitlements :- "$WIDGET" > "$SCRATCH/codesign.out" 2>"$SCRATCH/codesign.err" \
  || fail "exported widget signature could not be read"
security cms -D -i "$WIDGET/embedded.mobileprovision" > "$SCRATCH/widget-profile.plist" 2>"$SCRATCH/profile.log" \
  || fail "exported widget profile could not be read"
python3 - "$SCRATCH/codesign.out" "$SCRATCH/codesign.err" "$SCRATCH/widget-profile.plist" "$EXPECTED_PROFILE_UUID" <<'PY'
import plistlib
import sys
from pathlib import Path

stdout = Path(sys.argv[1])
stderr = Path(sys.argv[2])
profile_path = Path(sys.argv[3])
expected = sys.argv[4]
entitlements_data = stdout.read_bytes()
if not entitlements_data.strip():
    diagnostic = stderr.read_text(encoding="utf-8", errors="replace")
    start = diagnostic.find("<?xml")
    if start < 0:
        start = diagnostic.find("<plist")
    end = diagnostic.find("</plist>", start)
    if start < 0 or end < 0:
        raise SystemExit("exported widget signature or profile is invalid")
    entitlements_data = diagnostic[start : end + len("</plist>")].encode()
entitlements = plistlib.loads(entitlements_data)
profile = plistlib.loads(profile_path.read_bytes())
if (
    entitlements.get("application-identifier", "").endswith(".com.arnab.drover.widget")
    and "group.com.arnab.drover"
    in entitlements.get("com.apple.security.application-groups", [])
    and profile.get("UUID") == expected
    and profile.get("Entitlements", {}).get("application-identifier", "").endswith(
        ".com.arnab.drover.widget"
    )
):
    print(f"widget IPA verified: profile={expected} app-group=group.com.arnab.drover")
else:
    raise SystemExit("exported widget signature or profile is invalid")
PY
