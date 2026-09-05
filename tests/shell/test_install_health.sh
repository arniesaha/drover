#!/usr/bin/env bash
# The server can bind the private address selected during installation.  A
# loopback health check then reports a failure even though the phone can reach
# Drover.  Cover the bounded probe itself and the installer call that supplies
# its address.  The latter runs in a disposable home with a fake release so it
# never fetches software or touches a real service.
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

check_equal() {
  if [ "$2" = "$3" ]; then pass "$1"; else fail "$1 (got '$2', wanted '$3')"; fi
}

check_contains() {
  if /usr/bin/grep -F -q -- "$3" "$2"; then
    pass "$1"
  else
    fail "$1 (missing '$3')"
  fi
}

# A successful request must use the supplied private address, including when
# callers carry a trailing slash from a URL-like configuration value.
HEALTH_LOG="$WORK/health-success.log"
(
  . "$REPO/scripts/lib/health.sh"
  curl() {
    [ "$1" = '-fsS' ] && [ "$2" = '--max-time' ] && [ "$3" = '2' ] || return 97
    printf '%s\n' "$4" >> "$HEALTH_LOG"
    return 0
  }
  sleep() { :; }
  wait_for_health '100.64.0.10:7080/'
)
check_status "configured-address probe succeeds" "$?" "0"
check_equal "configured-address probe removes one trailing slash" \
  "$(/bin/cat "$HEALTH_LOG")" 'http://100.64.0.10:7080/healthz'

# A client failure must not look healthy and must preserve the existing retry
# budget.  curl -f makes non-2xx responses take this same failure path.
ATTEMPT_LOG="$WORK/health-failure.log"
(
  . "$REPO/scripts/lib/health.sh"
  curl() { printf 'curl\n' >> "$ATTEMPT_LOG"; return 22; }
  sleep() { printf '%s\n' "$1" >> "$ATTEMPT_LOG"; }
  if wait_for_health '100.64.0.10:7080'; then exit 1; fi
)
check_status "failed health probe returns failure" "$?" "0"
check_equal "failed health probe keeps fifteen attempts and two-second waits" \
  "$(/usr/bin/wc -l < "$ATTEMPT_LOG" | /usr/bin/tr -d ' ')" "30"
check_equal "failed health probe waits two seconds after every attempt" \
  "$(/usr/bin/grep -c '^2$' "$ATTEMPT_LOG")" "15"

# Exercise the copied-script path used by `curl ... | bash`.  The fakes only
# provide a disposable release layout; install.sh, the fetched helper files,
# and wait_for_health all run normally.
REMOTE="$WORK/remote"
FAKE_BIN="$REMOTE/bin"
HOME_DIR="$REMOTE/home"
mkdir -p "$FAKE_BIN" "$HOME_DIR"
/bin/cp "$REPO/install.sh" "$REMOTE/install.sh"

WHEEL_CONTENT='test wheel'
LOCK_CONTENT='test lock'
WHEEL_SHA="$(printf '%s' "$WHEEL_CONTENT" | /usr/bin/shasum -a 256 | /usr/bin/awk '{print $1}')"
LOCK_SHA="$(printf '%s' "$LOCK_CONTENT" | /usr/bin/shasum -a 256 | /usr/bin/awk '{print $1}')"
export FIXTURE_REPO="$REPO" FIXTURE_CURL_LOG="$REMOTE/curl.log"
export FIXTURE_WHEEL_CONTENT="$WHEEL_CONTENT" FIXTURE_LOCK_CONTENT="$LOCK_CONTENT"
export FIXTURE_WHEEL_SHA="$WHEEL_SHA" FIXTURE_LOCK_SHA="$LOCK_SHA"

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
  '    printf "helper %s\n" "$name" >> "$FIXTURE_CURL_LOG"' \
  '    /bin/cp "$FIXTURE_REPO/scripts/lib/$name" "$output"' \
  '    ;;' \
  '  */SHA256SUMS.txt)' \
  '    printf "%s  drover-0.0.0-py3-none-any.whl\n%s  requirements.lock.txt\n" "$FIXTURE_WHEEL_SHA" "$FIXTURE_LOCK_SHA" > "$output"' \
  '    ;;' \
  '  */drover-0.0.0-py3-none-any.whl) printf "%s" "$FIXTURE_WHEEL_CONTENT" > "$output" ;;' \
  '  */requirements.lock.txt) printf "%s" "$FIXTURE_LOCK_CONTENT" > "$output" ;;' \
  '  http://100.64.0.10:7080/healthz)' \
  '    printf "%s\n" "$url" >> "$FIXTURE_CURL_LOG"' \
  '    ;;' \
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
  '    /bin/cp "$(dirname "$0")/python" "$target/bin/python"' \
  '    /bin/cp "$(dirname "$0")/drover-server" "$target/bin/drover-server"' \
  '    chmod +x "$target/bin/python" "$target/bin/drover-server"' \
  '    ;;' \
  '  pip) ;;' \
  '  *) exit 1 ;;' \
  'esac' > "$FAKE_BIN/uv"
chmod +x "$FAKE_BIN/uv"

printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -eu' \
  'if [ "$1" != "-" ]; then exit 1; fi' \
  'shift' \
  'if [ "$1" = "darwin" ] || [ "$1" = "linux" ]; then' \
  '  os="$1"; home="$2"' \
  '  if [ "$os" = "darwin" ]; then' \
  '    mkdir -p "$home/Library/LaunchAgents"' \
  '    : > "$home/Library/LaunchAgents/com.drover.server.plist"' \
  '    : > "$home/Library/LaunchAgents/com.drover.harnessd.plist"' \
  '  fi' \
  'else' \
  '  config="$1"; address="$2"; host="$3"' \
  '  mkdir -p "$(dirname "$config")"' \
  '  printf "[server]\nadvertised_url = \"%s\"\nmetrics_host = \"%s\"\n" "$address" "$host" > "$config"' \
  'fi' > "$FAKE_BIN/python"
chmod +x "$FAKE_BIN/python"

printf '%s\n' \
  '#!/usr/bin/env bash' \
  'case "${1:-}" in' \
  '  --version) printf "0.0.0\n" ;;' \
  '  *) exit 0 ;;' \
  'esac' > "$FAKE_BIN/drover-server"
chmod +x "$FAKE_BIN/drover-server"

printf '%s\n' '#!/usr/bin/env bash' 'exit 0' > "$FAKE_BIN/launchctl"
chmod +x "$FAKE_BIN/launchctl"

printf '%s\n' '#!/usr/bin/env bash' 'exit 0' > "$FAKE_BIN/sleep"
chmod +x "$FAKE_BIN/sleep"

OUT="$(HOME="$HOME_DIR" PATH="$FAKE_BIN:$PATH" DROVER_OS=darwin \
  bash "$REMOTE/install.sh" --version 0.0.0 --url 100.64.0.10:7080 2>&1)"
check_status "remote-helper installer run succeeds" "$?" "0"
check_contains "remote-helper path fetches health helper" "$FIXTURE_CURL_LOG" 'helper health.sh'
check_contains "installer probes the configured address" "$FIXTURE_CURL_LOG" \
  'http://100.64.0.10:7080/healthz'

[ "$FAILURES" -eq 0 ] || exit 1
echo "all install-health checks passed"
