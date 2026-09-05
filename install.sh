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
# `BASH_SOURCE[0]` is unset for stdin on Bash 3.2; do not turn that into the
# current directory, which could make a piped install source unrelated files.
# When run from a checkout, prefer the local copies so the shell tests
# exercise exactly what is committed rather than what is published.
SELF_SOURCE="${BASH_SOURCE[0]:-}"
SELF_DIR=""
if [ -n "$SELF_SOURCE" ] && [ -f "$SELF_SOURCE" ]; then
  SELF_DIR="$(cd "$(dirname "$SELF_SOURCE")" && pwd)"
fi
if [ -n "$SELF_DIR" ] && [ -r "$SELF_DIR/scripts/lib/verify.sh" ]; then
  . "$SELF_DIR/scripts/lib/verify.sh"
  . "$SELF_DIR/scripts/lib/detect.sh"
  . "$SELF_DIR/scripts/lib/health.sh"
else
  LIB_TMP="$(mktemp -d)"
  trap 'rm -rf "$LIB_TMP"' EXIT
  for lib in verify detect health; do
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

# --- join URL ----------------------------------------------------------------
# Parsed in the main shell for the same reason validate_explicit_url is: a
# `fail` inside a command substitution exits only the subshell, so a refusal
# would print and the install would carry on regardless.
HUB_ADDRESS="" JOIN_CODE=""
parse_join_url() {
  local url="$1" query pair host
  case "$url" in
    drover://*) ;;
    *) fail "join URL must start with drover:// (got: $url)" ;;
  esac
  url="${url#drover://}"
  HUB_ADDRESS="${url%%\?*}"
  [ -n "$HUB_ADDRESS" ] || fail "join URL has no host"
  case "$url" in
    *\?*) query="${url#*\?}" ;;
    *)    query="" ;;
  esac
  # bash 3.2 has no associative arrays, so walk the pairs.
  local IFS='&'
  for pair in $query; do
    case "$pair" in
      code=*) JOIN_CODE="${pair#code=}" ;;
    esac
  done
  unset IFS
  [ -n "$JOIN_CODE" ] || fail "join URL has no code parameter"
  host="${HUB_ADDRESS%%:*}"
  is_private_address "$host" \
    || fail "$host is not a private address; refusing to join a public hub"
}

# --- address -----------------------------------------------------------------
# Validated in the main shell, deliberately NOT inside resolve_address: that
# runs in a command substitution, where `fail` exits only the subshell. The
# refusal would print and the install would carry on with a public address.
validate_explicit_url() {
  [ -n "$EXPLICIT_URL" ] || return 0
  local address host port port_value
  address="${EXPLICIT_URL#http://}"; address="${address#https://}"
  case "$address" in
    *:*)
      host="${address%%:*}"
      port="${address#*:}"
      # A TOML integer cannot preserve a leading-zero port, and a port outside
      # this range cannot name the listener that the config and unit advertise.
      case "$port" in
        ''|0|0[0-9]*|*[!0-9]*|??????*)
          fail "--url port must be an integer from 1 to 65535 (got: $port)" ;;
      esac
      port_value=$((10#$port))
      if [ "$port_value" -gt 65535 ]; then
        fail "--url port must be an integer from 1 to 65535 (got: $port)"
      fi
      ;;
    *) host="$address" ;;
  esac
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
[ -n "$JOIN_URL" ] && parse_join_url "$JOIN_URL"
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
  info "  would link drover-server into $HOME/.local/bin"
  if [ -n "$JOIN_URL" ]; then
    # Never echo the code itself: a join one-liner ends up in chat logs and
    # terminal scrollback often enough to matter, and it is a live credential
    # until it is redeemed or expires.
    info "  would join hub $HUB_ADDRESS with a single-use code"
    info "  would probe reachability, then register as direct or relay"
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
  # Two statements, not one. Under `set -u`, bash declares every name in a
  # single `local` before assigning any of them, so a later assignment that
  # expands an earlier one on the same line sees it unset and aborts —
  # "install.sh: line N: version: unbound variable", at install time, on
  # someone else's machine.
  local version="$1"
  local target="$DROVER_HOME/runtime/$version"
  local base="https://github.com/${REPO}/releases/download/v${version}"
  local tmp; tmp="$(mktemp -d)"

  curl -fsSL "$base/SHA256SUMS.txt" -o "$tmp/SHA256SUMS.txt" \
    || fail "release v${version} has no SHA256SUMS.txt (does it have artifacts?)"

  # Read the wheel's filename out of the manifest rather than building it from
  # the tag. A wheel is named after the version in pyproject.toml, which is
  # not guaranteed to equal the tag: constructing the name here would 404 on
  # any release where those drifted, and would do it only at install time on
  # someone else's machine.
  local wheel
  wheel="$(awk '$2 ~ /^\*?\.?\/?drover-.*-py3-none-any\.whl$/ {
                  sub(/^[*.]*\//, "", $2); sub(/^\*/, "", $2); print $2; exit }' \
           "$tmp/SHA256SUMS.txt")"
  [ -n "$wheel" ] \
    || fail "release v${version} lists no drover wheel in SHA256SUMS.txt"

  info "downloading ${wheel}..."
  curl -fsSL "$base/$wheel" -o "$tmp/$wheel" \
    || fail "could not download $wheel from release v${version}"
  curl -fsSL "$base/requirements.lock.txt" -o "$tmp/requirements.lock.txt" \
    || fail "could not download requirements.lock.txt"

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
  local address="$1" host="${1%%:*}" port="${1##*:}"
  [ -f "$DROVER_HOME/config.toml" ] \
    || "$DROVER_HOME/runtime/current/bin/drover-server" init >/dev/null 2>&1 || true
  # The bind and the advertised address live in config, never only in a unit's
  # argv: a regenerated unit that dropped the flag would silently revert the
  # server to loopback, which has happened before and is invisible until the
  # app stops loading.
  "$DROVER_HOME/runtime/current/bin/python" - "$DROVER_HOME/config.toml" \
    "$address" "$host" "$port" <<'PY'
import re, sys
path, advertised, host, port = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
try:
    text = open(path, encoding="utf-8").read()
except OSError:
    text = ""
if "[server]" not in text:
    text += "\n[server]\n"
def upsert(text, key, value):
    pattern = re.compile(rf'^{key}\s*=.*$', re.MULTILINE)
    line = f'{key} = {value}'
    if pattern.search(text):
        return pattern.sub(line, text)
    return re.sub(r'^\[server\]$', f'[server]\n{line}', text, count=1, flags=re.MULTILINE)
text = upsert(text, "advertised_url", f'"{advertised}"')
text = upsert(text, "metrics_host", f'"{host}"')
text = upsert(text, "metrics_http_port", port)
open(path, "w", encoding="utf-8").write(text)
PY
  success "config.toml points at $address"
}

# --- units -------------------------------------------------------------------
# install_units <mode> <central-url> [extra harnessd args]
#   mode "fleet" installs the hub and a local harnessd.
#   mode "join"  installs harnessd only: a joining machine must not start a
#                second hub, which would give the fleet two control planes.
install_units() {
  local mode="$1" central_url="$2" extra="${3:-}"
  local host_id; host_id="$(hostname -s 2>/dev/null || echo drover-host)"
  local bin="$DROVER_HOME/runtime/current/bin"
  "$bin/python" - "$OS" "$HOME" "$DROVER_HOME" "$host_id" "$mode" \
    "$central_url" "$extra" <<'PY'
import sys
from pathlib import Path
from drover.server.service_units import render_launchd, render_systemd, runtime_bin

os_name = sys.argv[1]
home = Path(sys.argv[2])
drover_home = Path(sys.argv[3])
host_id, mode, central_url, extra = sys.argv[4], sys.argv[5], sys.argv[6], sys.argv[7]

bin_dir = runtime_bin(drover_home)
path_entries = [str(bin_dir), "/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin"]

harnessd_args = ["--host-id", host_id, "--central-url", central_url]
harnessd_args += [part for part in extra.split() if part]

jobs = [("harnessd", str(bin_dir / "drover-harnessd"), harnessd_args)]
if mode == "fleet":
    jobs.insert(0, ("server", str(bin_dir / "drover-server"), ["run"]))

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

  local shorts="harnessd"
  [ "$mode" = "fleet" ] && shorts="server harnessd"

  if [ "$OS" = "darwin" ]; then
    local short
    for short in $shorts; do
      local plist="$HOME/Library/LaunchAgents/com.drover.$short.plist"
      launchctl unload "$plist" 2>/dev/null || true
      launchctl load -w "$plist" 2>/dev/null || true
    done
  else
    # Without lingering, user units stop at logout and never come back after a
    # reboot, which is the difference between a fleet host and a laptop.
    loginctl enable-linger "$USER" 2>/dev/null || true
    systemctl --user daemon-reload 2>/dev/null || true
    local short
    for short in $shorts; do
      systemctl --user enable --now "drover-$short.service" 2>/dev/null || true
    done
  fi
  success "services installed and started"
}

# --- CLI on PATH -------------------------------------------------------------
# Without this the installer finishes and `drover-server` is still not a
# command: everything lives under ~/.drover/runtime, which is on nobody's
# PATH. The pairing hint printed at the end used the absolute path for
# exactly that reason.
#
# The link points through runtime/current, never a version directory, for the
# same reason the service units must: a pinned path keeps resolving to the old
# build after an update, which is the trap that made the symlink flip a no-op
# on every existing host.
#
# Only drover-server. drover-harnessd is a daemon nobody types, and putting it
# on PATH is an invitation to start a second one by hand beside the managed
# unit.
link_cli() {
  local target="$DROVER_HOME/runtime/current/bin/drover-server"
  local bin_dir="$HOME/.local/bin"
  local link="$bin_dir/drover-server"

  mkdir -p "$bin_dir" || {
    warn "could not create $bin_dir; run drover-server from $target"
    return 0
  }

  # Never replace something this installer did not put there. A regular file
  # at that path is someone else's binary, and silently overwriting it is a
  # worse outcome than not installing ours.
  if [ -e "$link" ] && [ ! -L "$link" ]; then
    warn "$link exists and is not a symlink, so it was left alone"
    warn "run drover-server from $target instead"
    return 0
  fi

  # -f to replace our own older link, -n so an existing symlink-to-directory
  # is replaced rather than followed into.
  ln -sfn "$target" "$link" || {
    warn "could not link drover-server into $bin_dir"
    return 0
  }
  success "linked drover-server into $bin_dir"

  # The link can succeed and the command still not be found, which reads as
  # the installer having lied. Say so loudly, with the line to fix it.
  case ":$PATH:" in
    *":$bin_dir:"*) ;;
    *)
      echo
      warn "$bin_dir is not on your PATH, so 'drover-server' will not be found yet."
      warn "Add this to your shell profile, then open a new terminal:"
      printf '\n    export PATH="%s:$PATH"\n\n' "$bin_dir"
      ;;
  esac
}

# --- join --------------------------------------------------------------------
# Probe first, redeem second. The probe deliberately does not burn the code,
# so a machine that turns out to be unreachable can be retried without asking
# the hub for a fresh one.
join_fleet() {
  local host_id local_addr probe_body reachable listen_args paired token
  host_id="$(hostname -s 2>/dev/null || echo drover-host)"
  read -r _kind local_addr <<EOF
$(detect_address)
EOF
  local_addr="${local_addr%%:*}"

  info "asking the hub whether it can reach this machine..."
  # 7081 is harnessd's own port. If something is already listening there --
  # a re-run, or --adopt over an existing install -- it is already a better
  # answer to the probe than a throwaway server, and starting one would
  # either fail to bind or fight the daemon. Only start one if the port is
  # free, and only ever kill the pid we started ourselves.
  local probe_pid=""
  if lsof -nP -iTCP:7081 -sTCP:LISTEN >/dev/null 2>&1; then
    info "  something already answers on :7081; probing that"
  else
    "$DROVER_HOME/runtime/current/bin/python" -m http.server 7081 --bind 0.0.0.0 \
      >/dev/null 2>&1 &
    probe_pid=$!
    sleep 1
  fi
  probe_body="$(curl -fsS --max-time 15 -X POST \
    -H 'Content-Type: application/json' \
    -d "{\"code\":\"$JOIN_CODE\",\"url\":\"http://${local_addr}:7081\"}" \
    "http://${HUB_ADDRESS}/harness/probe" 2>/dev/null || echo '')"
  if [ -n "$probe_pid" ]; then
    kill "$probe_pid" 2>/dev/null || true
    wait "$probe_pid" 2>/dev/null || true
  fi

  case "$probe_body" in
    *'"reachable": true'*|*'"reachable":true'*) reachable=1 ;;
    *) reachable=0 ;;
  esac

  if [ "$reachable" -eq 1 ]; then
    success "hub can reach this machine; registering as a direct host"
    listen_args="--listen 0.0.0.0:7081 --local-url http://${local_addr}:7081"
  else
    success "hub cannot reach this machine; registering as a relay host"
    listen_args="--relay"
  fi

  info "redeeming the pairing code..."
  paired="$(curl -fsS --max-time 15 -X POST \
    -H 'Content-Type: application/json' \
    -d "{\"code\":\"$JOIN_CODE\",\"device_name\":\"$host_id\"}" \
    "http://${HUB_ADDRESS}/auth/pair" 2>/dev/null || echo '')"
  [ -n "$paired" ] || fail \
    "the hub refused the pairing code (expired, already used, or throttled). Ask for a fresh one with: drover-server pair-host --name $host_id"

  host_token="$(printf '%s' "$paired" \
    | "$DROVER_HOME/runtime/current/bin/python" -c \
      'import json,sys; print(json.load(sys.stdin)["token"])' 2>/dev/null || echo '')"
  [ -n "$host_token" ] || fail "the hub's pairing response was not understood"

  ( umask 077; printf "%s\n" "$host_token" > "$DROVER_HOME/api_token" )
  success "host credential stored"

  # Rewrite the harnessd unit with the connection mode the probe chose.
  install_units "$listen_args" "http://${HUB_ADDRESS}"
  success "joined $HUB_ADDRESS as $host_id"
}

ensure_uv
VERSION="$(resolve_version)"
info "  version:   $VERSION"
install_runtime "$VERSION"
# Both paths, not just the hub: a joined host runs harnessd only, but its
# operator still needs `drover-server --version` to see what it is running.
link_cli

if [ -n "$JOIN_URL" ]; then
  # A joining machine runs harnessd only and never writes a server config:
  # its control plane is the hub it is joining.
  join_fleet
  echo
  info "This machine is now part of the fleet at $HUB_ADDRESS."
  info "It will appear in the app shortly."
else
  write_config "$ADDRESS"
  install_units fleet "http://${ADDRESS}"
  if wait_for_health "$ADDRESS"; then
    success "drover-server is up"
  else
    warn "drover-server did not answer /healthz yet; check the logs before pairing"
  fi
  echo
  # Run through the link when it resolves, so the QR is printed by the same
  # command the user was just told they have. Falling back to the absolute
  # path keeps pairing working when ~/.local/bin is not on PATH yet.
  if command -v drover-server >/dev/null 2>&1; then
    drover-server pair || true
  else
    "$DROVER_HOME/runtime/current/bin/drover-server" pair || true
  fi
fi
