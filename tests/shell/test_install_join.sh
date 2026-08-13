#!/usr/bin/env bash
# The installer's --join path. Parse and refusal behaviour is covered here
# through --dry-run; the redeem-and-probe round trip needs a live hub and is
# covered by tests/test_harness_probe.py plus the manual rollout.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FAILURES=0
HOME_DIR="$WORK/home dir"
mkdir -p "$HOME_DIR"

check_contains() {
  if printf '%s' "$2" | grep -q -- "$3"; then
    echo "ok   - $1"
  else
    echo "FAIL - $1 (missing '$3')"
    FAILURES=$((FAILURES + 1))
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

run_join() {
  HOME="$HOME_DIR" DROVER_OS=linux \
    DROVER_TAILSCALE_CANDIDATES="$WORK/no-such-tailscale" \
    bash "$REPO/install.sh" --dry-run --join "$1" 2>&1
}

# --- happy path --------------------------------------------------------------
OUT="$(run_join 'drover://100.64.0.10:7080?v=1&code=H3TW-9KQ2')"; STATUS=$?
check_status "join dry run succeeds" "$STATUS" "0"
check_contains "reports the hub address" "$OUT" "100.64.0.10:7080"
check_contains "mentions joining" "$OUT" "join"
check_contains "mentions the probe" "$OUT" "probe"

# The code must never be echoed back in full: a join one-liner gets pasted
# into chat logs and terminal scrollback often enough to matter.
if printf '%s' "$OUT" | grep -q "H3TW-9KQ2"; then
  echo "FAIL - the pairing code is echoed in output"
  FAILURES=$((FAILURES + 1))
else
  echo "ok   - the pairing code is not echoed in output"
fi

# --- refusals ----------------------------------------------------------------
OUT="$(run_join 'https://evil.test?code=x')"; STATUS=$?
check_status "refuses a non-drover join URL" "$STATUS" "1"
check_contains "says what it wanted" "$OUT" "drover://"

OUT="$(run_join 'drover://100.64.0.10:7080?v=1')"; STATUS=$?
check_status "refuses a join URL with no code" "$STATUS" "1"
check_contains "says the code is missing" "$OUT" "code"

OUT="$(run_join 'drover://8.8.8.8:7080?v=1&code=X7QP2M4X')"; STATUS=$?
check_status "refuses a public hub address" "$STATUS" "1"
check_contains "says why" "$OUT" "private"

OUT="$(run_join 'drover://?v=1&code=X7QP2M4X')"; STATUS=$?
check_status "refuses a join URL with no host" "$STATUS" "1"

# A code carried in a later query position must still be found.
OUT="$(run_join 'drover://100.64.0.10:7080?v=1&n=home-fleet&code=H3TW-9KQ2')"
check_status "finds the code regardless of parameter order" "$?" "0"

# --- direct vs relay is decided by the probe, not by a flag ------------------
check_contains "explains that reachability decides direct vs relay" "$OUT" "relay"

[ "$FAILURES" -eq 0 ] || exit 1
echo "all checks passed"
