#!/usr/bin/env bash
# A new fleet must make its hub healthy before the local harness reads its
# credential.  Drive install.sh itself with fake release artefacts, supervisor
# commands, and health responses so the ordering is observable on both
# supported service managers without touching a real user service.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FAILURES=0

pass() { printf 'ok   - %s\n' "$1"; }
fail() { printf 'FAIL - %s\n' "$1"; FAILURES=$((FAILURES + 1)); }

check_status() {
  if [ "$2" = "$3" ]; then pass "$1"; else fail "$1 (exit $2, wanted $3)"; fi
}

check_contains() {
  if /usr/bin/grep -F -q -- "$3" "$2"; then
    pass "$1"
  else
    fail "$1 (missing '$3')"
  fi
}

check_event_absent() {
  if /usr/bin/grep -F -q -- "$3" "$2"; then
    fail "$1 (unexpected '$3')"
  else
    pass "$1"
  fi
}

event_line() {
  /usr/bin/grep -n -F -- "$2" "$1" | /usr/bin/head -n 1 | /usr/bin/cut -d: -f1
}

check_event_before() {
  local first
  local second
  first="$(event_line "$2" "$3")"
  second="$(event_line "$2" "$4")"
  if [ -n "$first" ] && [ -n "$second" ] && [ "$first" -lt "$second" ]; then
    pass "$1"
  else
    fail "$1 (wanted '$3' before '$4')"
  fi
}

REMOTE="$WORK/remote"
FAKE_BIN="$REMOTE/bin"
FAKE_SERVER="$FAKE_BIN/drover-server"
PYTHON="$(command -v python3)"
WHEEL_CONTENT='test wheel'
LOCK_CONTENT='test lock'
WHEEL_SHA="$(printf '%s' "$WHEEL_CONTENT" | /usr/bin/shasum -a 256 | /usr/bin/awk '{print $1}')"
LOCK_SHA="$(printf '%s' "$LOCK_CONTENT" | /usr/bin/shasum -a 256 | /usr/bin/awk '{print $1}')"
mkdir -p "$FAKE_BIN"

export FIXTURE_REPO="$REPO"
export FIXTURE_PYTHON="$PYTHON"
export FIXTURE_SERVER="$FAKE_SERVER"
export FIXTURE_WHEEL_CONTENT="$WHEEL_CONTENT"
export FIXTURE_LOCK_CONTENT="$LOCK_CONTENT"
export FIXTURE_WHEEL_SHA="$WHEEL_SHA"
export FIXTURE_LOCK_SHA="$LOCK_SHA"

printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -eu' \
  'url=""; output=""' \
  'while [ "$#" -gt 0 ]; do' \
  '  case "$1" in' \
  '    -o) output="$2"; shift 2 ;;' \
  '    --max-time) shift 2 ;;' \
  '    -f*|-s*) shift ;;' \
  '    *) url="$1"; shift ;;' \
  '  esac' \
  'done' \
  'case "$url" in' \
  '  https://raw.githubusercontent.com/*/scripts/lib/*.sh)' \
  '    name="${url##*/}"' \
  '    /bin/cp "$FIXTURE_REPO/scripts/lib/$name" "$output"' \
  '    ;;' \
  '  */SHA256SUMS.txt)' \
  '    printf "%s  drover-0.0.0-py3-none-any.whl\\n%s  requirements.lock.txt\\n" "$FIXTURE_WHEEL_SHA" "$FIXTURE_LOCK_SHA" > "$output"' \
  '    ;;' \
  '  */drover-0.0.0-py3-none-any.whl) printf "%s" "$FIXTURE_WHEEL_CONTENT" > "$output" ;;' \
  '  */requirements.lock.txt) printf "%s" "$FIXTURE_LOCK_CONTENT" > "$output" ;;' \
  '  */healthz)' \
  '    printf "health %s\\n" "$url" >> "$FIXTURE_EVENT_LOG"' \
  '    if [ "${FIXTURE_HEALTH_MODE:-delayed}" = "ready" ]; then' \
  '      printf "health-ready %s\\n" "$url" >> "$FIXTURE_EVENT_LOG"' \
  '      printf 204' \
  '      exit 0' \
  '    fi' \
  '    if [ "${FIXTURE_HEALTH_MODE:-delayed}" = "delayed" ]; then' \
  '      attempts="$(/usr/bin/grep -c "^health " "$FIXTURE_EVENT_LOG")"' \
  '      if [ "$attempts" -ge 2 ]; then' \
  '        printf "health-ready %s\\n" "$url" >> "$FIXTURE_EVENT_LOG"' \
  '        printf 204' \
  '        exit 0' \
  '      fi' \
  '    fi' \
  '    exit 22' \
  '    ;;' \
  '  */harness/probe) printf "{\"reachable\":false}" ;;' \
  '  */auth/pair) printf "{\"token\":\"test-token\"}" ;;' \
  '  *) exit 1 ;;' \
  'esac' > "$FAKE_BIN/curl"
chmod +x "$FAKE_BIN/curl"

printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -eu' \
  'case "$1" in' \
  '  venv)' \
  '    target="$2"' \
  '    mkdir -p "$target/bin"' \
  '    ln -sf "$FIXTURE_PYTHON" "$target/bin/python"' \
  '    ln -sf "$FIXTURE_SERVER" "$target/bin/drover-server"' \
  '    ;;' \
  '  pip) ;;' \
  '  *) exit 1 ;;' \
  'esac' > "$FAKE_BIN/uv"
chmod +x "$FAKE_BIN/uv"

printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -eu' \
  'case "${1:-}" in' \
  '  --version) printf "0.0.0\\n" ;;' \
  '  init) exit 0 ;;' \
  '  pair) printf "pair\\n" >> "$FIXTURE_EVENT_LOG" ;;' \
  '  *) exit 1 ;;' \
  'esac' > "$FAKE_SERVER"
chmod +x "$FAKE_SERVER"

printf '%s\n' \
  '#!/usr/bin/env bash' \
  'printf "systemctl %s\\n" "$*" >> "$FIXTURE_EVENT_LOG"' \
  'case "$*" in' \
  '  *"enable --now drover-server.service"*) printf "server-start\\n" >> "$FIXTURE_EVENT_LOG" ;;' \
  '  *"enable --now drover-harnessd.service"*) printf "harness-start\\n" >> "$FIXTURE_EVENT_LOG" ;;' \
  'esac' > "$FAKE_BIN/systemctl"
chmod +x "$FAKE_BIN/systemctl"

printf '%s\n' \
  '#!/usr/bin/env bash' \
  'if [ "${1:-}" = "load" ]; then' \
  '  case "$*" in' \
  '    *"com.drover.server.plist"*) printf "server-start\\n" >> "$FIXTURE_EVENT_LOG" ;;' \
  '    *"com.drover.harnessd.plist"*) printf "harness-start\\n" >> "$FIXTURE_EVENT_LOG" ;;' \
  '  esac' \
  'fi' > "$FAKE_BIN/launchctl"
chmod +x "$FAKE_BIN/launchctl"

printf '%s\n' '#!/usr/bin/env bash' 'exit 0' > "$FAKE_BIN/loginctl"
printf '%s\n' '#!/usr/bin/env bash' 'exit 1' > "$FAKE_BIN/lsof"
printf '%s\n' '#!/usr/bin/env bash' 'exit 0' > "$FAKE_BIN/sleep"
chmod +x "$FAKE_BIN/loginctl" "$FAKE_BIN/lsof" "$FAKE_BIN/sleep"

new_case() {
  local name
  name="$1"
  CASE_DIR="$REMOTE/$name"
  HOME_DIR="$CASE_DIR/home"
  EVENT_LOG="$CASE_DIR/events.log"
  mkdir -p "$HOME_DIR"
  : > "$EVENT_LOG"
  export FIXTURE_EVENT_LOG="$EVENT_LOG"
}

run_new_fleet() {
  local os
  local health_mode
  local address
  os="$1"
  health_mode="$2"
  address="$3"
  /bin/cat "$REPO/install.sh" | HOME="$HOME_DIR" PATH="$FAKE_BIN:$PATH" \
    PYTHONPATH="$REPO/src" DROVER_OS="$os" DROVER_TAILSCALE_CANDIDATES="$CASE_DIR/no-tailscale" \
    USER=installer FIXTURE_HEALTH_MODE="$health_mode" \
    bash -s -- --version 0.0.0 --url "$address" 2>&1
}

run_join() {
  /bin/cat "$REPO/install.sh" | HOME="$HOME_DIR" PATH="$FAKE_BIN:$PATH" \
    PYTHONPATH="$REPO/src" DROVER_OS=linux DROVER_TAILSCALE_CANDIDATES="$CASE_DIR/no-tailscale" \
    USER=installer FIXTURE_HEALTH_MODE=fail \
    bash -s -- --version 0.0.0 --join 'drover://100.64.0.10:7099?v=1&code=JOIN-CODE' 2>&1
}

run_order_case() {
  local os
  local label
  local unit
  local output
  local result
  os="$1"
  label="$2"
  new_case "order-$label"
  output="$(run_new_fleet "$os" delayed '100.64.0.10:7099')"
  result=$?
  check_status "$label delayed hub startup succeeds" "$result" "0"
  check_event_before "$label starts the hub before configured health" "$EVENT_LOG" \
    'server-start' 'health-ready http://100.64.0.10:7099/healthz'
  check_event_before "$label starts the harness after configured health" "$EVENT_LOG" \
    'health-ready http://100.64.0.10:7099/healthz' 'harness-start'
  if [ "$os" = linux ]; then
    unit="$HOME_DIR/.config/systemd/user/drover-harnessd.service"
  else
    unit="$HOME_DIR/Library/LaunchAgents/com.drover.harnessd.plist"
  fi
  check_contains "$label harness uses the configured hub URL" "$unit" \
    'http://100.64.0.10:7099'
}

run_failure_case() {
  local os
  local label
  local output
  local result
  os="$1"
  label="$2"
  new_case "failure-$label"
  output="$(run_new_fleet "$os" fail '100.64.0.10:7099')"
  result=$?
  check_status "$label fails when hub health does not arrive" "$result" "1"
  check_contains "$label reports the unavailable hub" <(printf '%s' "$output") \
    'drover-server did not answer /healthz'
  check_event_absent "$label never starts an unauthenticated harness" "$EVENT_LOG" \
    'harness-start'
  check_event_absent "$label does not present pairing after failed health" "$EVENT_LOG" 'pair'
}

run_order_case linux systemd
run_order_case darwin launchd
run_failure_case linux systemd
run_failure_case darwin launchd

new_case join
OUT="$(run_join)"
RESULT=$?
check_status "join install succeeds without a hub health gate" "$RESULT" "0"
check_event_absent "join does not start a local hub" "$EVENT_LOG" 'server-start'
check_event_absent "join does not probe the hub health endpoint" "$EVENT_LOG" 'health '
check_contains "join starts only its harness" "$EVENT_LOG" 'harness-start'

[ "$FAILURES" -eq 0 ] || exit 1
echo "all install-startup checks passed"
