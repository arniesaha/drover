#!/usr/bin/env bash
# Exercise the public curl-pipe installer against a disposable release layout.
# The release fetch, runtime installation, and service-manager commands are
# faked, but install.sh's inline config Python and systemd rendering run from
# the checked-out source. This catches a published installer that appears to
# finish while emitting a Bash error before it has selected its helper source.
set -uo pipefail

# Select the caller's Python (including an activated venv), then discard
# runner settings before invoking any fixture or installer subprocess.
if [ "${1:-}" != --isolated ]; then
  exec /usr/bin/env -i HOME="$HOME" PATH=/usr/bin:/bin \
    TMPDIR="${TMPDIR:-/tmp}" FIXTURE_PYTHON="$(command -v python3)" \
    USER=installer /bin/bash "$0" --isolated
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
. "$HERE/lib/install_fixture.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FAILURES=0

pass() { printf 'ok   - %s\n' "$1"; }
fail() {
  printf 'FAIL - %s\n' "$1"
  FAILURES=$((FAILURES + 1))
  if [ -s "${INSTALL_LOG:-}" ]; then
    printf 'installer stdout/stderr:\n'
    /bin/cat "$INSTALL_LOG"
  fi
}

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
OFFLINE_BIN="$REMOTE/offline bin"
HOME_DIR="$REMOTE/home dir"
UNRELATED="$REMOTE/unrelated checkout"
mkdir -p "$FAKE_BIN" "$OFFLINE_BIN" "$HOME_DIR/.drover" "$UNRELATED/scripts/lib"

# A piped script must not treat this working directory as its own checkout.
printf '%s\n' '#!/usr/bin/env bash' 'exit 99' > "$UNRELATED/scripts/lib/verify.sh"
chmod +x "$UNRELATED/scripts/lib/verify.sh"

WHEEL_CONTENT='test wheel'
LOCK_CONTENT='test lock'
WHEEL_SHA="$(printf '%s' "$WHEEL_CONTENT" | /usr/bin/shasum -a 256 | /usr/bin/awk '{print $1}')"
LOCK_SHA="$(printf '%s' "$LOCK_CONTENT" | /usr/bin/shasum -a 256 | /usr/bin/awk '{print $1}')"
PYTHON="$FIXTURE_PYTHON"
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

# Invalid explicit ports must fail before helper resolution. This offline curl
# records any mistaken helper download rather than allowing test fixtures to
# hide an ordering regression.
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'printf "curl %s\\n" "$*" >> "$FIXTURE_OFFLINE_CURL_LOG"' \
  'exit 97' > "$OFFLINE_BIN/curl"
chmod +x "$OFFLINE_BIN/curl"

printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -eu' \
  'case "$1" in' \
  '  venv)' \
  '    if [ "${2:-}" = "--relocatable" ]; then target="$3"; else target="$2"; fi' \
  '    printf "venv\\n" >> "$FIXTURE_UV_LOG"' \
  '    mkdir -p "$target/bin"' \
  '    printf '\''#!/bin/bash\nexec "$FIXTURE_PYTHON" "$@"\n'\'' > "$target/bin/python"' \
  '    chmod +x "$target/bin/python"' \
  '    ln -s "$(dirname "$0")/drover-server" "$target/bin/drover-server"' \
  '    ln -s "$(dirname "$0")/drover-server" "$target/bin/drover"' \
  '    ln -s "$(dirname "$0")/drover-server" "$target/bin/drover-harnessd"' \
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
  '  control-store) [ "${2:-}" = "init" ] && [ "${3:-}" = "--help" ] && exit 0; exit 1 ;;' \
  '  *) exit 1 ;;' \
  'esac' > "$FAKE_BIN/drover-server"
chmod +x "$FAKE_BIN/drover-server"

printf '%s\n' '#!/usr/bin/env bash' 'exit 0' > "$FAKE_BIN/loginctl"
printf '%s\n' '#!/usr/bin/env bash' 'exit 0' > "$FAKE_BIN/systemctl"
printf '%s\n' '#!/usr/bin/env bash' 'exit 0' > "$FAKE_BIN/sleep"
printf '%s\n' '#!/usr/bin/env bash' 'printf "installer-host\\n"' > "$FAKE_BIN/hostname"
chmod +x "$FAKE_BIN/loginctl" "$FAKE_BIN/systemctl" "$FAKE_BIN/sleep" "$FAKE_BIN/hostname"

# Keep an unrelated setting in the real config-generation seam. Later checks
# will assert that the inline Python preserves it while updating server keys.
printf '%s\n' \
  '[server]' \
  'preserved_setting = "keep"' \
  'metrics_http_port = 7080' > "$HOME_DIR/.drover/config.toml"

run_piped_install() {
  local home="$1" url="$2" bin="${3:-$FAKE_BIN}"
  HOME="$home" PATH="$bin:$PATH" PYTHONPATH="$REPO/src" DROVER_OS=linux \
    DROVER_TAILSCALE_CANDIDATES="$REMOTE/no-tailscale" \
    run_installer --version 0.0.0 --url "$url"
}

INSTALL_LOG="$REMOTE/install.log"
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
  local curl_log="$REMOTE/invalid port $label curl.log"
  local output result
  mkdir -p "$home"
  INSTALL_LOG="$REMOTE/invalid port $label install.log"
  export FIXTURE_UV_LOG="$uv_log"
  export FIXTURE_OFFLINE_CURL_LOG="$curl_log"
  output="$(cd "$UNRELATED" && run_piped_install "$home" "100.64.0.10:$port" "$OFFLINE_BIN")"
  result=$?
  check_status "invalid $label port is rejected" "$result" "1"
  check_contains "invalid $label port explains the accepted range" \
    <(printf '%s' "$output") 'integer from 1 to 65535'
  check_absent "invalid $label port does not attempt an offline helper download" \
    "$( [ -e "$curl_log" ] && echo exists || echo absent )" 'exists'
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

# Force the producer past any pipe buffer and inherit ignored SIGPIPE, as a
# runner can do. Without restoring the signal, cat reports EPIPE as exit 1
# instead of 141, which must not turn a successful installer into a failure.
check_early_exit() {
  local label="$1" expected="$2" message="$3"
  local case_dir="$REMOTE/early $label"
  local home="$case_dir/home dir"
  local output result
  mkdir -p "$home"
  {
    printf "printf '%s\\n' >&2; exit %s\n" "$message" "$expected"
    /usr/bin/awk 'BEGIN { for (i = 0; i < 100000; i++) print "# unread installer input" }'
  } > "$case_dir/install.sh"
  INSTALL_LOG="$case_dir/install.log"
  output="$(trap '' PIPE; HOME="$home" REPO="$case_dir" run_installer)"
  result=$?
  check_status "early piped $label reports the installer status" "$result" "$expected"
  check_contains "early piped $label captures installer stderr" "$INSTALL_LOG" "$message"
  if [ "$label" = success ]; then
    check_absent "early piped success has no refusal stderr" "$output" 'fixture early refusal'
  fi
}

check_early_exit refusal 1 'fixture early refusal'
check_early_exit success 0 'fixture early success'

# A missing input is a genuine producer failure, unlike the consumer closing
# the pipe early. Keep that failure visible even though bash sees empty stdin.
MISSING_REPO="$REMOTE/missing input"
mkdir -p "$MISSING_REPO"
INSTALL_LOG="$MISSING_REPO/install.log"
OUT="$(REPO="$MISSING_REPO" run_installer)"
RESULT=$?
check_status "missing installer input is rejected" "$RESULT" "1"
check_contains "missing installer input reports the producer failure" "$INSTALL_LOG" \
  'installer input failed (exit 1)'

[ "$FAILURES" -eq 0 ] || exit 1
echo "all install-bootstrap checks passed"
