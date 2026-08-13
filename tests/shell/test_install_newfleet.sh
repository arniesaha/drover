#!/usr/bin/env bash
# The installer's new-fleet path, exercised through --dry-run so nothing
# touches the network or a real home directory.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FAILURES=0

check_contains() {
  if printf '%s' "$2" | grep -q -- "$3"; then
    echo "ok   - $1"
  else
    echo "FAIL - $1 (missing '$3')"
    FAILURES=$((FAILURES + 1))
  fi
}
check_absent() {
  if printf '%s' "$2" | grep -q -- "$3"; then
    echo "FAIL - $1 (unexpectedly found '$3')"
    FAILURES=$((FAILURES + 1))
  else
    echo "ok   - $1"
  fi
}
check_status() {
  if [ "$2" = "$3" ]; then
    echo "ok   - $1"
  else
    echo "FAIL - $1 (exit $2, wanted $3)"
    FAILURES=$((FAILURES + 1))
  fi
}

# A private home whose path contains a space, matching the real dev checkout.
HOME_DIR="$WORK/home dir"
mkdir -p "$HOME_DIR"

run_install() {
  HOME="$HOME_DIR" DROVER_OS=darwin \
    DROVER_TAILSCALE_CANDIDATES="$WORK/no-such-tailscale" \
    bash "$REPO/install.sh" --dry-run "$@" 2>&1
}

# --- happy path --------------------------------------------------------------
OUT="$(run_install --url 100.64.0.10:7080)"; STATUS=$?
check_status "dry run succeeds" "$STATUS" "0"
check_contains "reports the runtime path" "$OUT" ".drover/runtime"
check_contains "honours an explicit --url" "$OUT" "100.64.0.10:7080"
check_contains "writes advertised_url" "$OUT" "advertised_url"
check_contains "installs the server unit" "$OUT" "com.drover.server"
check_contains "installs the harnessd unit" "$OUT" "com.drover.harnessd"
check_contains "ends by pairing" "$OUT" "pair"
check_absent "changes nothing on a dry run" "$OUT" "Installed to"

# Nothing may be written during a dry run.
check_status "dry run created no ~/.drover" \
  "$([ -e "$HOME_DIR/.drover" ] && echo exists || echo absent)" "absent"
check_status "dry run created no launch agent" \
  "$([ -e "$HOME_DIR/Library/LaunchAgents" ] && echo exists || echo absent)" "absent"

# --- refusals ----------------------------------------------------------------
OUT="$(run_install --url http://8.8.8.8:7080)"; STATUS=$?
check_status "refuses a public --url" "$STATUS" "1"
check_contains "says why it refused" "$OUT" "private"

OUT="$(HOME="$HOME_DIR" DROVER_OS=plan9 bash "$REPO/install.sh" --dry-run 2>&1)"
check_status "refuses an unsupported OS" "$?" "1"

OUT="$(run_install --nonsense)"; STATUS=$?
check_status "refuses an unknown flag" "$STATUS" "1"

# --- linux renders systemd rather than launchd -------------------------------
OUT="$(HOME="$HOME_DIR" DROVER_OS=linux \
  DROVER_TAILSCALE_CANDIDATES="$WORK/no-such-tailscale" \
  bash "$REPO/install.sh" --dry-run --url 10.0.0.5:7080 2>&1)"
check_contains "linux installs systemd units" "$OUT" "drover-server.service"
check_contains "linux enables lingering" "$OUT" "linger"
check_absent "linux does not mention launchd" "$OUT" "LaunchAgents"

# --- loopback warning --------------------------------------------------------
# Stub the LAN lookup to fail, so this is deterministic rather than depending
# on whether the machine running the suite happens to have a LAN address.
mkdir -p "$WORK/nolan"
printf '#!/usr/bin/env bash\nexit 1\n' > "$WORK/nolan/ipconfig"
chmod +x "$WORK/nolan/ipconfig"
OUT="$(HOME="$HOME_DIR" DROVER_OS=darwin PATH="$WORK/nolan:$PATH" \
  DROVER_TAILSCALE_CANDIDATES="$WORK/no-such-tailscale" \
  bash "$REPO/install.sh" --dry-run 2>&1)"
check_contains "warns when only loopback is available" "$OUT" "loopback"
check_contains "explains the loopback consequence" "$OUT" "only this machine"

# --- adoption ----------------------------------------------------------------
mkdir -p "$HOME_DIR/Library/LaunchAgents"
cat > "$HOME_DIR/Library/LaunchAgents/com.drover.harnessd.plist" <<'PLIST'
<plist><dict><key>ProgramArguments</key><array>
<string>/Users/someone/src/drover/.venv/bin/drover-harnessd</string>
</array></dict></plist>
PLIST

OUT="$(run_install --url 100.64.0.10:7080)"; STATUS=$?
check_status "refuses to clobber an existing install" "$STATUS" "1"
check_contains "names the file it found" "$OUT" "com.drover.harnessd.plist"
check_contains "offers --adopt" "$OUT" "--adopt"

OUT="$(run_install --url 100.64.0.10:7080 --adopt)"; STATUS=$?
check_status "--adopt proceeds" "$STATUS" "0"

# A unit this installer wrote must not trigger the refusal.
cat > "$HOME_DIR/Library/LaunchAgents/com.drover.harnessd.plist" <<PLIST
<plist><dict><key>ProgramArguments</key><array>
<string>$HOME_DIR/.drover/runtime/current/bin/drover-harnessd</string>
</array></dict></plist>
PLIST
OUT="$(run_install --url 100.64.0.10:7080)"; STATUS=$?
check_status "an installer-written unit is not foreign" "$STATUS" "0"

[ "$FAILURES" -eq 0 ] || exit 1
echo "all checks passed"
