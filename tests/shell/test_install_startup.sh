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

check_absent() {
  if /usr/bin/grep -F -q -- "$3" "$2"; then
    fail "$1 (unexpected '$3')"
  else
    pass "$1"
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

check_event_count() {
  local actual
  actual="$(/usr/bin/grep -F -c -- "$3" "$2" || true)"
  if [ "$actual" = "$4" ]; then
    pass "$1"
  else
    fail "$1 (found $actual '$3' events, wanted $4)"
  fi
}

REMOTE="$WORK/remote"
FAKE_BIN="$REMOTE/bin"
FAKE_SERVER="$FAKE_BIN/drover-server"
PYTHON="$(command -v python3)"
INCOMPATIBLE_RUNTIME_PYTHON="$FAKE_BIN/incompatible-runtime-python"
WHEEL_CONTENT='test wheel'
LOCK_CONTENT='test lock'
WHEEL_SHA="$(printf '%s' "$WHEEL_CONTENT" | /usr/bin/shasum -a 256 | /usr/bin/awk '{print $1}')"
LOCK_SHA="$(printf '%s' "$LOCK_CONTENT" | /usr/bin/shasum -a 256 | /usr/bin/awk '{print $1}')"
mkdir -p "$FAKE_BIN"

export FIXTURE_REPO="$REPO"
export FIXTURE_PYTHON="$PYTHON"
export FIXTURE_INCOMPATIBLE_RUNTIME_PYTHON="$INCOMPATIBLE_RUNTIME_PYTHON"
export FIXTURE_SERVER="$FAKE_SERVER"
export FIXTURE_WHEEL_CONTENT="$WHEEL_CONTENT"
export FIXTURE_LOCK_CONTENT="$LOCK_CONTENT"
export FIXTURE_WHEEL_SHA="$WHEEL_SHA"
export FIXTURE_LOCK_SHA="$LOCK_SHA"

printf '%s\n' '#!/usr/bin/env bash' 'cat >/dev/null' 'exit 1' \
  > "$INCOMPATIBLE_RUNTIME_PYTHON"
chmod +x "$INCOMPATIBLE_RUNTIME_PYTHON"

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
  '    if [ "${2:-}" = "--relocatable" ]; then target="$3"; else target="$2"; fi' \
  '    mkdir -p "$target/bin"' \
  '    if [ "${FIXTURE_RUNTIME_CAPABILITY:-postgres-default}" = "legacy" ]; then' \
  '      ln -sf "$FIXTURE_INCOMPATIBLE_RUNTIME_PYTHON" "$target/bin/python"' \
  '    else' \
  '      ln -sf "$FIXTURE_PYTHON" "$target/bin/python"' \
  '    fi' \
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
  '  init)' \
  '    mkdir -p "$HOME/.drover"' \
  '    printf "%s\\n" "[control_store]" "backend = \"postgres\"" "dsn_env = \"DROVER_CONTROL_DSN\"" > "$HOME/.drover/config.toml"' \
  '    ;;' \
  '  control-store)' \
  '    case "${2:-}" in' \
  '      init)' \
  '        if [ "${3:-}" = "--help" ]; then exit 0; fi' \
  '        [ -n "${DROVER_CONTROL_DSN:-}" ] || { printf "missing control DSN\\n" >&2; exit 1; }' \
  '        [ "${FIXTURE_CONTROL_INIT_MODE:-ready}" != "fail" ] || { printf "control init failed\\n" >&2; exit 1; }' \
  '        if [ -n "${FIXTURE_EXPECTED_DSN+x}" ]; then [ "$DROVER_CONTROL_DSN" = "$FIXTURE_EXPECTED_DSN" ] || exit 1; fi' \
  '        printf "control-init\\n" >> "$FIXTURE_EVENT_LOG"' \
  '        ;;' \
  '      status) printf "control-status\\n" >> "$FIXTURE_EVENT_LOG"; echo '\''{"ready":true}'\'' ;;' \
  '      *) exit 1 ;;' \
  '    esac' \
  '    ;;' \
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
  shift 3
  local dsn="${FIXTURE_INSTALL_DSN-postgresql://fixture-control}"
  /bin/cat "$REPO/install.sh" | HOME="$HOME_DIR" PATH="$FAKE_BIN:$PATH" \
    PYTHONPATH="$REPO/src" DROVER_OS="$os" DROVER_TAILSCALE_CANDIDATES="$CASE_DIR/no-tailscale" \
    USER=installer FIXTURE_HEALTH_MODE="$health_mode" DROVER_CONTROL_DSN="$dsn" \
    bash -s -- --version 0.0.0 --url "$address" "$@" 2>&1
}

run_existing_fleet_without_dsn() {
  local os
  local address
  os="$1"
  address="$2"
  /bin/cat "$REPO/install.sh" | HOME="$HOME_DIR" PATH="$FAKE_BIN:$PATH" \
    PYTHONPATH="$REPO/src" DROVER_OS="$os" DROVER_TAILSCALE_CANDIDATES="$CASE_DIR/no-tailscale" \
    USER=installer FIXTURE_HEALTH_MODE=ready DROVER_CONTROL_DSN="" \
    bash -s -- --version 0.0.0 --url "$address" --no-start 2>&1
}

run_join() {
  /bin/cat "$REPO/install.sh" | HOME="$HOME_DIR" PATH="$FAKE_BIN:$PATH" \
    PYTHONPATH="$REPO/src" DROVER_OS=linux DROVER_TAILSCALE_CANDIDATES="$CASE_DIR/no-tailscale" \
    USER=installer FIXTURE_HEALTH_MODE=fail \
    bash -s -- --version 0.0.0 --join 'drover://100.64.0.10:7099?v=1&code=JOIN-CODE' "$@" 2>&1
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
  check_event_before "$label initializes the PostgreSQL control store before hub startup" "$EVENT_LOG" \
    'control-init' 'server-start'
  if [ "$os" = linux ]; then
    unit="$HOME_DIR/.config/systemd/user/drover-harnessd.service"
    server_unit="$HOME_DIR/.config/systemd/user/drover-server.service"
  else
    unit="$HOME_DIR/Library/LaunchAgents/com.drover.harnessd.plist"
    server_unit="$HOME_DIR/Library/LaunchAgents/com.drover.server.plist"
  fi
  check_contains "$label harness uses the configured hub URL" "$unit" \
    'http://100.64.0.10:7099'
  check_contains "$label config selects PostgreSQL" "$HOME_DIR/.drover/config.toml" \
    'backend = "postgres"'
  check_contains "$label config names the PostgreSQL environment variable" "$HOME_DIR/.drover/config.toml" \
    'dsn_env = "DROVER_CONTROL_DSN"'
  check_contains "$label stores the PostgreSQL DSN privately" "$HOME_DIR/.drover/server.env" \
    'DROVER_CONTROL_DSN='
  check_absent "$label config does not contain the PostgreSQL DSN" "$HOME_DIR/.drover/config.toml" \
    'postgresql://fixture-control'
  check_absent "$label unit does not contain the PostgreSQL DSN" "$server_unit" \
    'postgresql://fixture-control'
  output="$(run_new_fleet "$os" ready '100.64.0.10:7099')"
  result=$?
  check_status "$label existing PostgreSQL install validates before restart" "$result" "0"
  check_contains "$label checks existing PostgreSQL readiness" "$EVENT_LOG" 'control-status'
  check_event_count "$label does not reinitialize an existing PostgreSQL store" "$EVENT_LOG" \
    'control-init' 1
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

new_case escaped-dsn
export FIXTURE_INSTALL_DSN='host=127.0.0.1 application_name=has space password=it'\''s\\path $literal `tick` "double"'
export FIXTURE_EXPECTED_DSN="$FIXTURE_INSTALL_DSN"
OUT="$(run_new_fleet linux ready '100.64.0.10:7099' --no-start)"
RESULT=$?
unset FIXTURE_EXPECTED_DSN FIXTURE_INSTALL_DSN
check_status "fresh install preserves an escaped PostgreSQL DSN" "$RESULT" "0"
check_contains "escaped PostgreSQL DSN is stored in a private environment file" \
  "$HOME_DIR/.drover/server.env" 'DROVER_CONTROL_DSN='

new_case missing-dsn
export FIXTURE_INSTALL_DSN=""
OUT="$(run_new_fleet linux ready '100.64.0.10:7099')"
RESULT=$?
unset FIXTURE_INSTALL_DSN
check_status "fresh install refuses a missing PostgreSQL DSN" "$RESULT" "1"
check_contains "missing PostgreSQL DSN is actionable" <(printf '%s' "$OUT") 'DROVER_CONTROL_DSN'
check_status "missing PostgreSQL DSN does not write a config" \
  "$([ -e "$HOME_DIR/.drover/config.toml" ] && echo present || echo absent)" "absent"
check_event_absent "missing PostgreSQL DSN does not initialize a store" "$EVENT_LOG" 'control-init'
check_event_absent "missing PostgreSQL DSN does not start the hub" "$EVENT_LOG" 'server-start'

new_case incompatible-runtime-fresh
export FIXTURE_RUNTIME_CAPABILITY=legacy
OUT="$(run_new_fleet linux ready '100.64.0.10:7099')"
RESULT=$?
unset FIXTURE_RUNTIME_CAPABILITY
check_status "incompatible pinned runtime rejects fresh install" "$RESULT" "1"
check_contains "incompatible pinned runtime explains PostgreSQL-default requirement" \
  <(printf '%s' "$OUT") 'predates PostgreSQL-default setup'
check_status "incompatible fresh runtime leaves current absent" \
  "$([ -e "$HOME_DIR/.drover/runtime/current" ] && echo present || echo absent)" "absent"
check_status "incompatible fresh runtime leaves config absent" \
  "$([ -e "$HOME_DIR/.drover/config.toml" ] && echo present || echo absent)" "absent"
check_status "incompatible fresh runtime leaves private environment absent" \
  "$([ -e "$HOME_DIR/.drover/server.env" ] && echo present || echo absent)" "absent"
check_status "incompatible fresh runtime leaves units absent" \
  "$([ -e "$HOME_DIR/.config/systemd/user/drover-server.service" ] && echo present || echo absent)" "absent"

new_case incompatible-runtime-existing-postgres
mkdir -p "$HOME_DIR/.drover/runtime/previous" "$HOME_DIR/.config/systemd/user"
ln -s previous "$HOME_DIR/.drover/runtime/current"
printf '%s\n' '[control_store]' 'backend = "postgres"' 'dsn_env = "DROVER_CONTROL_DSN"' \
  > "$HOME_DIR/.drover/config.toml"
printf '%s\n' 'DROVER_CONTROL_DSN="postgresql://prior"' > "$HOME_DIR/.drover/server.env"
printf '%s\n' 'prior server unit' > "$HOME_DIR/.config/systemd/user/drover-server.service"
PREVIOUS_CONFIG="$(/bin/cat "$HOME_DIR/.drover/config.toml")"
PREVIOUS_ENV="$(/bin/cat "$HOME_DIR/.drover/server.env")"
PREVIOUS_UNIT="$(/bin/cat "$HOME_DIR/.config/systemd/user/drover-server.service")"
export FIXTURE_RUNTIME_CAPABILITY=legacy
OUT="$(run_new_fleet linux ready '100.64.0.10:7099')"
RESULT=$?
unset FIXTURE_RUNTIME_CAPABILITY
check_status "incompatible pinned runtime rejects existing PostgreSQL install" "$RESULT" "1"
check_status "incompatible existing runtime keeps current target" \
  "$(readlink "$HOME_DIR/.drover/runtime/current")" "previous"
check_status "incompatible existing runtime keeps PostgreSQL config" \
  "$(/bin/cat "$HOME_DIR/.drover/config.toml")" "$PREVIOUS_CONFIG"
check_status "incompatible existing runtime keeps private environment" \
  "$(/bin/cat "$HOME_DIR/.drover/server.env")" "$PREVIOUS_ENV"
check_status "incompatible existing runtime keeps server unit" \
  "$(/bin/cat "$HOME_DIR/.config/systemd/user/drover-server.service")" "$PREVIOUS_UNIT"

new_case incompatible-runtime-join
mkdir -p "$HOME_DIR/.drover/runtime/previous" "$HOME_DIR/.config/systemd/user"
ln -s previous "$HOME_DIR/.drover/runtime/current"
printf '%s\n' 'existing host unit' > "$HOME_DIR/.config/systemd/user/drover-harnessd.service"
PREVIOUS_JOIN_UNIT="$(/bin/cat "$HOME_DIR/.config/systemd/user/drover-harnessd.service")"
export FIXTURE_RUNTIME_CAPABILITY=legacy
OUT="$(run_join)"
RESULT=$?
unset FIXTURE_RUNTIME_CAPABILITY
check_status "incompatible pinned runtime rejects join" "$RESULT" "1"
check_status "incompatible join runtime keeps current target" \
  "$(readlink "$HOME_DIR/.drover/runtime/current")" "previous"
check_status "incompatible join runtime keeps host unit" \
  "$(/bin/cat "$HOME_DIR/.config/systemd/user/drover-harnessd.service")" "$PREVIOUS_JOIN_UNIT"

new_case control-init-failure
export FIXTURE_CONTROL_INIT_MODE=fail
OUT="$(run_new_fleet linux ready '100.64.0.10:7099')"
RESULT=$?
unset FIXTURE_CONTROL_INIT_MODE
check_status "failed PostgreSQL initialization stops installation" "$RESULT" "1"
check_contains "failed PostgreSQL initialization is actionable" <(printf '%s' "$OUT") \
  'PostgreSQL control-store initialization failed'
check_event_absent "failed PostgreSQL initialization does not start the hub" "$EVENT_LOG" 'server-start'
check_event_absent "failed PostgreSQL initialization does not start the harness" "$EVENT_LOG" 'harness-start'

new_case reused-private-env
OUT="$(run_new_fleet linux ready '100.64.0.10:7099' --no-start)"
RESULT=$?
check_status "fresh install creates a reusable private environment" "$RESULT" "0"
chmod 0644 "$HOME_DIR/.drover/server.env"
OUT="$(run_existing_fleet_without_dsn linux '100.64.0.10:7099')"
RESULT=$?
check_status "existing PostgreSQL install accepts its private environment" "$RESULT" "0"
MODE="$($PYTHON -c 'import os, stat, sys; print(oct(stat.S_IMODE(os.stat(sys.argv[1]).st_mode)))' "$HOME_DIR/.drover/server.env")"
check_status "existing PostgreSQL environment is owner-only" "$MODE" "0o600"

new_case no-start
OUT="$(run_new_fleet linux fail '100.64.0.10:7099' --no-start)"
RESULT=$?
check_status "no-start install succeeds without a ready hub" "$RESULT" "0"
check_contains "no-start writes the configured server address" \
  "$HOME_DIR/.drover/config.toml" 'metrics_host = "100.64.0.10"'
check_status "no-start renders the server service definition" \
  "$([ -f "$HOME_DIR/.config/systemd/user/drover-server.service" ] && echo present || echo absent)" "present"
check_status "no-start renders the harness service definition" \
  "$([ -f "$HOME_DIR/.config/systemd/user/drover-harnessd.service" ] && echo present || echo absent)" "present"
check_contains "no-start says first-use readiness was skipped" <(printf '%s' "$OUT") \
  'automation mode'
check_event_absent "no-start does not start the hub" "$EVENT_LOG" 'server-start'
check_event_absent "no-start does not start the harness" "$EVENT_LOG" 'harness-start'
check_event_absent "no-start does not check hub readiness" "$EVENT_LOG" 'health '
check_event_absent "no-start does not print pairing" "$EVENT_LOG" 'pair'

new_case join
OUT="$(run_join)"
RESULT=$?
check_status "join install succeeds without a hub health gate" "$RESULT" "0"
check_event_absent "join does not start a local hub" "$EVENT_LOG" 'server-start'
check_event_absent "join does not probe the hub health endpoint" "$EVENT_LOG" 'health '
check_contains "join starts only its harness" "$EVENT_LOG" 'harness-start'

new_case no-start-join
OUT="$(run_join --no-start)"
RESULT=$?
check_status "no-start refuses a join install" "$RESULT" "1"
check_contains "no-start join refusal explains the incompatible flags" <(printf '%s' "$OUT") \
  '--no-start cannot be used with --join'

[ "$FAILURES" -eq 0 ] || exit 1
echo "all install-startup checks passed"
