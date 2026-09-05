#!/usr/bin/env bash
# The server can bind the private address selected during installation.  A
# loopback health check then reports a failure even though the phone can reach
# Drover.  Cover the bounded probe against a local HTTP fixture and the
# installer call that supplies its address.  The latter runs in a disposable
# home with a fake release so it never fetches software or touches a real
# service.
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

# A real local server distinguishes curl's transport result from the HTTP
# status.  For 302, /healthz redirects to /ready, which returns 204: following
# redirects would therefore hide the redirect from the probe.
PYTHON="$(command -v python3)"
run_http_health_case() {
  local response_code="$1"
  local expected="$2"
  local name="$3"
  local port_file="$WORK/http-$response_code.port"
  local server_pid
  local port
  local result

  "$PYTHON" - "$response_code" "$port_file" <<'PY' &
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

response_code = int(sys.argv[1])
port_file = Path(sys.argv[2])

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz" and response_code == 302:
            self.send_response(302)
            self.send_header("Location", "/ready")
        elif self.path == "/healthz":
            self.send_response(response_code)
        else:
            self.send_response(204)
        self.end_headers()

    def log_message(self, format, *args):
        pass

server = HTTPServer(("127.0.0.1", 0), Handler)
port_file.write_text(str(server.server_port))
server.serve_forever()
PY
  server_pid=$!

  for _ in 1 2 3 4 5 6 7 8 9 10; do
    [ -s "$port_file" ] && break
    /bin/sleep 0.1
  done
  if [ ! -s "$port_file" ]; then
    kill "$server_pid" 2>/dev/null || true
    wait "$server_pid" 2>/dev/null || true
    fail "$name (local HTTP fixture did not start)"
    return
  fi
  port="$(/bin/cat "$port_file")"

  (
    . "$REPO/scripts/lib/health.sh"
    sleep() { :; }
    if [ "$expected" = success ]; then
      wait_for_health "127.0.0.1:${port}/"
    elif wait_for_health "127.0.0.1:${port}"; then
      exit 1
    fi
  )
  result=$?
  kill "$server_pid" 2>/dev/null || true
  wait "$server_pid" 2>/dev/null || true
  check_status "$name" "$result" "0"
}

run_http_health_case 204 success "2xx health response succeeds"
run_http_health_case 302 failure "3xx health redirect is rejected"
run_http_health_case 404 failure "4xx health response is rejected"
run_http_health_case 500 failure "5xx health response is rejected"

# A transport failure must remain a bounded failed probe.
ATTEMPT_LOG="$WORK/health-transport.log"
(
  . "$REPO/scripts/lib/health.sh"
  curl() { printf 'curl\n' >> "$ATTEMPT_LOG"; return 7; }
  sleep() { printf '%s\n' "$1" >> "$ATTEMPT_LOG"; }
  if wait_for_health '100.64.0.10:7080'; then exit 1; fi
)
check_status "transport failure returns failure" "$?" "0"
check_equal "transport failure keeps fifteen attempts and two-second waits" \
  "$(/usr/bin/wc -l < "$ATTEMPT_LOG" | /usr/bin/tr -d ' ')" "30"
check_equal "transport failure waits two seconds after every attempt" \
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
  '    printf 204' \
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
