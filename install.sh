#!/usr/bin/env bash
# Drover installer.
#
#   curl -fsSL https://raw.githubusercontent.com/arniesaha/drover/main/install.sh | bash
#   curl -fsSL … | bash -s -- --join 'drover://100.64.0.10:7080?v=1&code=H3TW-9KQ2'
#
# Targets bash 3.2, the version macOS ships: no associative arrays, no ${x^^},
# no mapfile. Every path is quoted, because a home or checkout containing a
# space is normal and unquoted paths fail there in ways that look like a bug
# in Drover rather than in this script.
set -euo pipefail

REPO="arniesaha/drover"
DROVER_HOME="${HOME}/.drover"
JOIN_URL="" EXPLICIT_URL="" ADOPT=0 DRY_RUN=0 WANT_VERSION=""

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'
CYAN=$'\033[0;36m'; NC=$'\033[0m'
info()    { printf '%s%s%s\n' "$CYAN" "$1" "$NC"; }
success() { printf '%s✓ %s%s\n' "$GREEN" "$1" "$NC"; }
warn()    { printf '%s⚠ %s%s\n' "$YELLOW" "$1" "$NC"; }
fail()    { printf '%s✗ %s%s\n' "$RED" "$1" "$NC" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --join)    JOIN_URL="${2:-}"; shift 2 ;;
    --url)     EXPLICIT_URL="${2:-}"; shift 2 ;;
    --version) WANT_VERSION="${2:-}"; shift 2 ;;
    --adopt)   ADOPT=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help)
      sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) fail "unknown flag: $1" ;;
  esac
done

OS="${DROVER_OS:-$(uname -s | tr '[:upper:]' '[:lower:]')}"
case "$OS" in
  darwin|linux) ;;
  *) fail "unsupported platform: $OS (macOS and Linux only)" ;;
esac

# When piped from curl there is no checkout on disk, so fetch the helpers.
# When run from a checkout, prefer the local copies so the shell tests
# exercise exactly what is committed rather than what is published.
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -r "$SELF_DIR/scripts/lib/verify.sh" ]; then
  . "$SELF_DIR/scripts/lib/verify.sh"
  . "$SELF_DIR/scripts/lib/detect.sh"
else
  LIB_TMP="$(mktemp -d)"
  trap 'rm -rf "$LIB_TMP"' EXIT
  for lib in verify detect; do
    curl -fsSL "https://raw.githubusercontent.com/${REPO}/main/scripts/lib/${lib}.sh" \
      -o "$LIB_TMP/${lib}.sh" || fail "could not fetch scripts/lib/${lib}.sh"
    . "$LIB_TMP/${lib}.sh"
  done
fi

# --- refuse to clobber a hand-rolled install ---------------------------------
# Someone running this on a machine that already has a source-built Drover
# should get a clear refusal, not a surprise migration. --adopt is the
# explicit opt-in; it leaves ~/.drover state untouched either way.
check_existing_install() {
  local found=""
  if [ "$OS" = "darwin" ]; then
    local plist
    for plist in "$HOME/Library/LaunchAgents"/com.drover.*.plist; do
      [ -e "$plist" ] || continue
      grep -q "$DROVER_HOME/runtime" "$plist" 2>/dev/null && continue
      found="$plist"; break
    done
  else
    local unit
    for unit in "$HOME/.config/systemd/user"/drover-*.service; do
      [ -e "$unit" ] || continue
      grep -q "$DROVER_HOME/runtime" "$unit" 2>/dev/null && continue
      found="$unit"; break
    done
  fi
  [ -n "$found" ] || return 0
  if [ "$ADOPT" -eq 1 ]; then
    warn "adopting existing install: $found"
    return 0
  fi
  printf '%s✗ found an existing Drover service this installer did not create:%s\n' \
    "$RED" "$NC" >&2
  printf '    %s\n\n' "$found" >&2
  printf '  It points outside %s/runtime, so replacing it would be a surprise.\n' \
    "$DROVER_HOME" >&2
  printf '  Re-run with --adopt to migrate it. Your config, token, and database\n' >&2
  printf '  are preserved either way.\n' >&2
  exit 1
}

# --- address -----------------------------------------------------------------
# Validated in the main shell, deliberately NOT inside resolve_address: that
# runs in a command substitution, where `fail` exits only the subshell. The
# refusal would print and the install would carry on with a public address.
validate_explicit_url() {
  [ -n "$EXPLICIT_URL" ] || return 0
  local address host
  address="${EXPLICIT_URL#http://}"; address="${address#https://}"
  host="${address%%:*}"
  is_private_address "$host" \
    || fail "$host is not a private address; Drover must not be published publicly"
}

resolve_address() {
  local kind address
  if [ -n "$EXPLICIT_URL" ]; then
    address="${EXPLICIT_URL#http://}"; address="${address#https://}"
    case "$address" in
      *:*) printf 'explicit %s\n' "$address" ;;
      *)   printf 'explicit %s:7080\n' "$address" ;;
    esac
    return 0
  fi
  read -r kind address <<EOF
$(detect_address)
EOF
  case "$kind" in
    tailscale|lan) printf '%s %s:7080\n' "$kind" "$address" ;;
    *)             printf 'loopback 127.0.0.1:7080\n' ;;
  esac
}

check_existing_install
validate_explicit_url
read -r ADDRESS_KIND ADDRESS <<EOF
$(resolve_address)
EOF

info "Drover installer"
info "  platform:  $OS"
info "  address:   $ADDRESS ($ADDRESS_KIND)"
info "  runtime:   $DROVER_HOME/runtime"
if [ "$ADDRESS_KIND" = "loopback" ]; then
  warn "no private address found; only this machine will reach the server"
fi

if [ "$DRY_RUN" -eq 1 ]; then
  info "  would write $DROVER_HOME/config.toml with [server] advertised_url"
  if [ "$OS" = "darwin" ]; then
    info "  would install $HOME/Library/LaunchAgents/com.drover.server.plist"
    info "  would install $HOME/Library/LaunchAgents/com.drover.harnessd.plist"
  else
    info "  would install ~/.config/systemd/user/drover-server.service"
    info "  would install ~/.config/systemd/user/drover-harnessd.service"
    info "  would enable linger so the units survive logout"
  fi
  if [ -n "$JOIN_URL" ]; then
    info "  would join via $JOIN_URL"
  else
    info "  would run: drover-server pair"
  fi
  success "dry run complete; nothing was changed"
  exit 0
fi

# --- uv ----------------------------------------------------------------------
ensure_uv() {
  command -v uv >/dev/null 2>&1 && return 0
  info "installing uv..."
  curl -fsSL https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 || true
  export PATH="$HOME/.local/bin:$PATH"
  command -v uv >/dev/null 2>&1 \
    || fail "uv is required but could not be installed; see https://docs.astral.sh/uv/"
  success "uv installed"
}

# --- version -----------------------------------------------------------------
resolve_version() {
  if [ -n "$WANT_VERSION" ]; then
    printf '%s' "${WANT_VERSION#v}"
    return 0
  fi
  local tag
  tag="$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" 2>/dev/null \
    | grep '"tag_name"' | sed -E 's/.*"([^"]+)".*/\1/' | head -1)"
  [ -n "$tag" ] || fail "could not determine the latest release of ${REPO}"
  printf '%s' "${tag#v}"
}

# --- install -----------------------------------------------------------------
install_runtime() {
  local version="$1" target="$DROVER_HOME/runtime/$version"
  local wheel="drover-${version}-py3-none-any.whl"
  local base="https://github.com/${REPO}/releases/download/v${version}"
  local tmp; tmp="$(mktemp -d)"

  info "downloading ${wheel}..."
  curl -fsSL "$base/$wheel" -o "$tmp/$wheel" \
    || fail "could not download $wheel (does release v${version} have artifacts?)"
  curl -fsSL "$base/requirements.lock.txt" -o "$tmp/requirements.lock.txt" \
    || fail "could not download requirements.lock.txt"
  curl -fsSL "$base/SHA256SUMS.txt" -o "$tmp/SHA256SUMS.txt" \
    || fail "could not download SHA256SUMS.txt"

  verify_against_manifest "$tmp/$wheel" "$wheel" "$tmp/SHA256SUMS.txt" \
    || fail "refusing to install: $wheel failed checksum verification"
  verify_against_manifest "$tmp/requirements.lock.txt" "requirements.lock.txt" \
    "$tmp/SHA256SUMS.txt" \
    || fail "refusing to install: requirements.lock.txt failed verification"
  success "artifacts verified"

  mkdir -p "$DROVER_HOME/runtime"
  uv venv "$target" >/dev/null 2>&1 || fail "could not create a venv at $target"
  # Dependencies are hash-pinned; the wheel is installed --no-deps because it
  # has already been verified against the manifest itself.
  uv pip install --python "$target/bin/python" --require-hashes \
    -r "$tmp/requirements.lock.txt" >/dev/null \
    || fail "dependency install failed (hash mismatch?)"
  uv pip install --python "$target/bin/python" --no-deps "$tmp/$wheel" >/dev/null \
    || fail "installing $wheel failed"
  rm -rf "$tmp"

  # A version that cannot state its own version never gets the symlink.
  "$target/bin/drover-server" --version >/dev/null 2>&1 \
    || fail "$version failed its smoke test; leaving the current install alone"

  ln -sfn "$version" "$DROVER_HOME/runtime/.current.new"
  mv -f "$DROVER_HOME/runtime/.current.new" "$DROVER_HOME/runtime/current"
  success "installed $version"
}

# --- config ------------------------------------------------------------------
write_config() {
  local address="$1" host="${1%%:*}"
  [ -f "$DROVER_HOME/config.toml" ] \
    || "$DROVER_HOME/runtime/current/bin/drover-server" init >/dev/null 2>&1 || true
  # The bind and the advertised address live in config, never only in a unit's
  # argv: a regenerated unit that dropped the flag would silently revert the
  # server to loopback, which has happened before and is invisible until the
  # app stops loading.
  "$DROVER_HOME/runtime/current/bin/python" - "$DROVER_HOME/config.toml" \
    "$address" "$host" <<'PY'
import re, sys
path, advertised, host = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    text = open(path, encoding="utf-8").read()
except OSError:
    text = ""
if "[server]" not in text:
    text += "\n[server]\n"
def upsert(text, key, value):
    pattern = re.compile(rf'^{key}\s*=.*$', re.MULTILINE)
    line = f'{key} = "{value}"'
    if pattern.search(text):
        return pattern.sub(line, text)
    return re.sub(r'^\[server\]$', f'[server]\n{line}', text, count=1, flags=re.MULTILINE)
text = upsert(text, "advertised_url", advertised)
text = upsert(text, "metrics_host", host)
open(path, "w", encoding="utf-8").write(text)
PY
  success "config.toml points at $address"
}

# --- units -------------------------------------------------------------------
install_units() {
  local host_id; host_id="$(hostname -s 2>/dev/null || echo drover-host)"
  local bin="$DROVER_HOME/runtime/current/bin"
  "$bin/python" - "$OS" "$HOME" "$DROVER_HOME" "$host_id" <<'PY'
import os, sys
from pathlib import Path
from drover.server.service_units import render_launchd, render_systemd, runtime_bin

os_name, home, drover_home, host_id = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
bin_dir = runtime_bin(drover_home)
path_entries = [str(bin_dir), "/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin"]

jobs = [
    ("server", str(bin_dir / "drover-server"), ["run"]),
    ("harnessd", str(bin_dir / "drover-harnessd"),
     ["--host-id", host_id, "--central-url", "http://127.0.0.1:7080"]),
]
if os_name == "darwin":
    target = home / "Library" / "LaunchAgents"
    target.mkdir(parents=True, exist_ok=True)
    (home / "Library" / "Logs" / "drover").mkdir(parents=True, exist_ok=True)
    for short, program, args in jobs:
        label = f"com.drover.{short}"
        (target / f"{label}.plist").write_text(
            render_launchd(label, program, args, home=home, path_entries=path_entries)
        )
        print(str(target / f"{label}.plist"))
else:
    target = home / ".config" / "systemd" / "user"
    target.mkdir(parents=True, exist_ok=True)
    for short, program, args in jobs:
        (target / f"drover-{short}.service").write_text(
            render_systemd(f"Drover {short}", program, args, path_entries=path_entries)
        )
        print(str(target / f"drover-{short}.service"))
PY

  if [ "$OS" = "darwin" ]; then
    local label
    for label in com.drover.server com.drover.harnessd; do
      launchctl unload "$HOME/Library/LaunchAgents/$label.plist" 2>/dev/null || true
      launchctl load -w "$HOME/Library/LaunchAgents/$label.plist" 2>/dev/null || true
    done
  else
    # Without lingering, user units stop at logout and never come back after a
    # reboot, which is the difference between a fleet host and a laptop.
    loginctl enable-linger "$USER" 2>/dev/null || true
    systemctl --user daemon-reload 2>/dev/null || true
    systemctl --user enable --now drover-server.service drover-harnessd.service \
      2>/dev/null || true
  fi
  success "services installed and started"
}

wait_for_health() {
  local i
  for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    if curl -fsS --max-time 2 "http://127.0.0.1:7080/healthz" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

ensure_uv
VERSION="$(resolve_version)"
info "  version:   $VERSION"
install_runtime "$VERSION"
write_config "$ADDRESS"
install_units

if wait_for_health; then
  success "drover-server is up"
else
  warn "drover-server did not answer /healthz yet; check the logs before pairing"
fi

if [ -n "$JOIN_URL" ]; then
  join_fleet "$JOIN_URL"
else
  echo
  "$DROVER_HOME/runtime/current/bin/drover-server" pair || true
fi
