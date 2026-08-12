#!/usr/bin/env bash
# Work out what address a phone should dial.
#
# Order is Tailscale, then the private LAN, then loopback. Tailscale first
# because it is the only one that keeps working when the user leaves the
# house, which is most of what a fleet cockpit is for. A public address is
# never returned: Drover is not meant to be published, and an installer that
# quietly advertised one would be handing out the fleet.
#
# Sourced, not executed. Echoes "<kind> <address>".

is_private_address() {
  case "$1" in
    10.*|192.168.*|127.*) return 0 ;;
    172.1[6-9].*|172.2[0-9].*|172.3[0-1].*) return 0 ;;
    # Tailscale's CGNAT range is 100.64.0.0/10, so 100.64 through 100.127.
    # Note ipaddress.ip_address().is_private in Python returns False for
    # these, which is why this is spelled out rather than delegated.
    100.6[4-9].*|100.[7-9][0-9].*|100.1[0-1][0-9].*|100.12[0-7].*) return 0 ;;
    *) return 1 ;;
  esac
}

# Find the Tailscale CLI.
#
# Only the Homebrew install puts `tailscale` on PATH. The macOS app ships the
# CLI inside its bundle and merely offers to symlink it, which many people
# skip -- so checking PATH alone silently hands a LAN-only address to exactly
# the users who most need the tailnet. Verified on this machine: a live
# 100.97.x.x tailnet address with no `tailscale` on PATH.
_tailscale_bin() {
  if command -v tailscale >/dev/null 2>&1; then
    command -v tailscale
    return 0
  fi
  # Overridable so tests can isolate from the host's real install, and so a
  # user with an unusual install can point at it without editing this file.
  local candidates="${DROVER_TAILSCALE_CANDIDATES:-\
/Applications/Tailscale.app/Contents/MacOS/Tailscale \
/usr/local/bin/tailscale \
/opt/homebrew/bin/tailscale \
$HOME/.local/bin/tailscale}"
  local candidate
  for candidate in $candidates; do
    if [ -x "$candidate" ]; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  return 1
}

_tailscale_address() {
  local bin
  bin="$(_tailscale_bin)" || return 1
  # `status` failing is how a logged-out tailnet presents; the binary being
  # installed says nothing about whether it is usable.
  "$bin" status >/dev/null 2>&1 || return 1
  local address
  address="$("$bin" ip -4 2>/dev/null | head -1 | tr -d '[:space:]')"
  [ -n "$address" ] || return 1
  printf '%s' "$address"
}

_lan_address() {
  local os="${DROVER_OS:-$(uname -s | tr '[:upper:]' '[:lower:]')}"
  local address=""
  if [ "$os" = "darwin" ]; then
    local interface
    for interface in en0 en1 en2; do
      address="$(ipconfig getifaddr "$interface" 2>/dev/null)"
      [ -n "$address" ] && break
    done
  else
    # Ask the routing table which source address would reach the internet,
    # rather than guessing an interface name.
    address="$(ip -4 route get 1.1.1.1 2>/dev/null \
      | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}' | head -1)"
  fi
  [ -n "$address" ] || return 1
  printf '%s' "$address"
}

detect_address() {
  local address
  if address="$(_tailscale_address)"; then
    printf 'tailscale %s\n' "$address"
    return 0
  fi
  if address="$(_lan_address)" && is_private_address "$address"; then
    printf 'lan %s\n' "$address"
    return 0
  fi
  printf 'loopback 127.0.0.1\n'
}
