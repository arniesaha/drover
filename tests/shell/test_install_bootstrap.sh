#!/usr/bin/env bash
# Exercise the public curl-pipe installer against a disposable release layout.
# The release fetch, runtime installation, and service-manager commands are
# faked, but install.sh's inline config Python and systemd rendering run from
# the checked-out source. This catches a published installer that appears to
# finish while emitting a Bash error before it has selected its helper source.
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

check_absent() {
  if printf '%s' "$2" | /usr/bin/grep -F -q -- "$3"; then
    fail "$1 (unexpected '$3')"
  else
    pass "$1"
  fi
}

check_contains() {
  if /usr/bin/grep -F -q -- "$3" "$2"; then
    pass "$1"
  else
    fail "$1 (missing '$3')"
  fi
}

REMOTE="$WORK/remote"
FAKE_BIN="$REMOTE/bin"
HOME_DIR="$REMOTE/home dir"
UNRELATED="$REMOTE/unrelated checkout"
mkdir -p "$FAKE_BIN" "$HOME_DIR/.drover" "$UNRELATED/scripts/lib"

# A piped script must not treat this working directory as its own checkout.
printf '%s\n' '#!/usr/bin/env bash' 'exit 99' > "$UNRELATED/scripts/lib/verify.sh"
chmod +x "$UNRELATED/scripts/lib/verify.sh"

WHEEL_CONTENT='test wheel'
LOCK_CONTENT='test lock'
WHEEL_SHA="$(printf '%s' "$WHEEL_CONTENT" | /usr/bin/shasum -a 256 | /usr/bin/awk '{print $1}')"
LOCK_SHA="$(printf '%s' "$LOCK_CONTENT" | /usr/bin/shasum -a 256 | /usr/bin/awk '{print $1}')"
PYTHON="$(command -v python3)"
export FIXTURE_REPO="$REPO" FIXTURE_CURL_LOG="$REMOTE/curl.log"
export FIXTURE_WHEEL_CONTENT="$WHEEL_CONTENT" FIXTURE_LOCK_CONTENT="$LOCK_CONTENT"
export FIXTURE_WHEEL_SHA="$WHEEL_SHA" FIXTURE_LOCK_SHA="$LOCK_SHA"
export FIXTURE_UV_LOG="$REMOTE/uv.log" FIXTURE_PYTHON="$PYTHON"

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
  '    printf "helper %s\\n" "$name" >> "$FIXTURE_CURL_LOG"' \
  '    /bin/cp "$FIXTURE_REPO/scripts/lib/$name" "$output"' \
  '    ;;' \
  '  */SHA256SUMS.txt)' \
  '    printf "%s  drover-0.0.0-py3-none-any.whl\\n%s  requirements.lock.txt\\n" "$FIXTURE_WHEEL_SHA" "$FIXTURE_LOCK_SHA" > "$output"' \
  '    ;;' \
  '  */drover-0.0.0-py3-none-any.whl) printf "%s" "$FIXTURE_WHEEL_CONTENT" > "$output" ;;' \
  '  */requirements.lock.txt) printf "%s" "$FIXTURE_LOCK_CONTENT" > "$output" ;;' \
  '  http://100.64.0.10:7099/healthz) printf "health %s\\n" "$url" >> "$FIXTURE_CURL_LOG"; printf 204 ;;' \
  '  *) exit 1 ;;' \
  'esac' > "$FAKE_BIN/curl"
chmod +x "$FAKE_BIN/curl"

printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -eu' \
  'case "$1" in' \
  '  venv)' \
  '    target="$2"' \
  '    printf "venv\\n" >> "$FIXTURE_UV_LOG"' \
  '    mkdir -p "$target/bin"' \
  '    ln -s "$FIXTURE_PYTHON" "$target/bin/python"' \
  '    ln -s "$(dirname "$0")/drover-server" "$target/bin/drover-server"' \
  '    ;;' \
  '  pip) printf "pip\\n" >> "$FIXTURE_UV_LOG" ;;' \
  '  *) exit 1 ;;' \
  'esac' > "$FAKE_BIN/uv"
chmod +x "$FAKE_BIN/uv"

printf '%s\n' \
  '#!/usr/bin/env bash' \
  'case "${1:-}" in' \
  '  --version) printf "0.0.0\\n" ;;' \
  '  init|pair) exit 0 ;;' \
  '  *) exit 1 ;;' \
  'esac' > "$FAKE_BIN/drover-server"
chmod +x "$FAKE_BIN/drover-server"

printf '%s\n' '#!/usr/bin/env bash' 'exit 0' > "$FAKE_BIN/loginctl"
printf '%s\n' '#!/usr/bin/env bash' 'exit 0' > "$FAKE_BIN/systemctl"
printf '%s\n' '#!/usr/bin/env bash' 'exit 0' > "$FAKE_BIN/sleep"
chmod +x "$FAKE_BIN/loginctl" "$FAKE_BIN/systemctl" "$FAKE_BIN/sleep"

# Keep an unrelated setting in the real config-generation seam. Later checks
# will assert that the inline Python preserves it while updating server keys.
printf '%s\n' \
  '[server]' \
  'preserved_setting = "keep"' \
  'metrics_http_port = 7080' > "$HOME_DIR/.drover/config.toml"

run_piped_install() {
  local home="$1" url="$2"
  /bin/cat "$REPO/install.sh" | HOME="$home" PATH="$FAKE_BIN:$PATH" \
    PYTHONPATH="$REPO/src" DROVER_OS=linux \
    bash -s -- --version 0.0.0 --url "$url" 2>&1
}

OUT="$(cd "$UNRELATED" && run_piped_install "$HOME_DIR" '100.64.0.10:7099')"
RESULT=$?
check_status "piped installer succeeds" "$RESULT" "0"
check_absent "piped installer has no BASH_SOURCE error" "$OUT" 'BASH_SOURCE[0]: unbound variable'
check_contains "piped installer fetches verify helper" "$FIXTURE_CURL_LOG" 'helper verify.sh'
check_contains "piped installer fetches detect helper" "$FIXTURE_CURL_LOG" 'helper detect.sh'
check_contains "piped installer fetches health helper" "$FIXTURE_CURL_LOG" 'helper health.sh'
check_contains "explicit port updates the server listener configuration" \
  "$HOME_DIR/.drover/config.toml" 'metrics_http_port = 7099'
check_contains "real config rewrite preserves unrelated server settings" \
  "$HOME_DIR/.drover/config.toml" 'preserved_setting = "keep"'
check_contains "config advertises the configured endpoint" \
  "$HOME_DIR/.drover/config.toml" 'advertised_url = "100.64.0.10:7099"'
check_contains "health checks the configured endpoint" "$FIXTURE_CURL_LOG" \
  'health http://100.64.0.10:7099/healthz'
check_contains "fleet harness unit registers with the configured hub" \
  "$HOME_DIR/.config/systemd/user/drover-harnessd.service" \
  '--central-url http://100.64.0.10:7099'

check_invalid_port() {
  local port="$1" label="$2"
  local home="$REMOTE/invalid port $label home"
  local uv_log="$REMOTE/invalid port $label uv.log"
  local output result
  mkdir -p "$home"
  export FIXTURE_UV_LOG="$uv_log"
  output="$(cd "$UNRELATED" && run_piped_install "$home" "100.64.0.10:$port")"
  result=$?
  check_status "invalid $label port is rejected" "$result" "1"
  check_contains "invalid $label port explains the accepted range" \
    <(printf '%s' "$output") 'integer from 1 to 65535'
  check_absent "invalid $label port does not create a runtime" \
    "$( [ -e "$home/.drover/runtime" ] && echo exists || echo absent )" 'exists'
  check_absent "invalid $label port does not write config" \
    "$( [ -e "$home/.drover/config.toml" ] && echo exists || echo absent )" 'exists'
  check_absent "invalid $label port does not invoke uv" \
    "$( [ -e "$uv_log" ] && echo exists || echo absent )" 'exists'
}

check_invalid_port 0 zero
check_invalid_port 65536 oversized
check_invalid_port nope nonnumeric

[ "$FAILURES" -eq 0 ] || exit 1
echo "all install-bootstrap checks passed"
