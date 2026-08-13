#!/usr/bin/env bash
# Where should a phone dial? Tailscale, else private LAN, else loopback.
# Public addresses are never acceptable: Drover must not be published.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/../../scripts/lib/detect.sh"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FAILURES=0

check() {
  if [ "$2" = "$3" ]; then
    echo "ok   - $1"
  else
    echo "FAIL - $1 (expected '$3', got '$2')"
    FAILURES=$((FAILURES + 1))
  fi
}

# --- is_private_address ------------------------------------------------------
for addr in 192.168.1.5 10.0.0.9 172.16.4.1 172.31.255.254 127.0.0.1 \
            100.64.0.10 100.127.255.254; do
  is_private_address "$addr"
  check "private: $addr" "$?" "0"
done

# 172.32/12 is outside RFC1918; 100.128+ is outside the CGNAT range Tailscale
# uses. Both are easy off-by-one mistakes in a case pattern.
for addr in 8.8.8.8 172.32.0.1 172.15.0.1 203.0.113.7 100.128.0.1 100.63.255.254; do
  is_private_address "$addr"
  check "public: $addr" "$?" "1"
done

# --- detect_address ----------------------------------------------------------
stub() { printf '#!/usr/bin/env bash\n%s\n' "$2" > "$WORK/$1"; chmod +x "$WORK/$1"; }

# Isolate from whatever Tailscale this host actually has installed. Without
# this, the "absent tailscale" cases below find the real /Applications binary
# and the suite passes or fails depending on the machine.
export DROVER_TAILSCALE_CANDIDATES="$WORK/no-such-tailscale"

# Tailscale up wins.
stub tailscale 'case "$1" in
  status) echo "100.64.0.10  host  linux  -" ;;
  ip)     echo "100.64.0.10" ;;
esac'
check "tailscale detected" "$(PATH="$WORK:$PATH" detect_address)" "tailscale 100.64.0.10"

# Logged out must not win, even though the binary exists.
stub tailscale 'case "$1" in
  status) echo "Logged out."; exit 1 ;;
  ip)     exit 1 ;;
esac'
stub ipconfig 'echo "192.168.1.5"'
check "falls back to LAN when tailscale is logged out" \
  "$(PATH="$WORK:$PATH" DROVER_OS=darwin detect_address)" "lan 192.168.1.5"

# A public LAN address is not usable and must not be advertised.
stub ipconfig 'echo "203.0.113.7"'
check "refuses a public LAN address" \
  "$(PATH="$WORK:$PATH" DROVER_OS=darwin detect_address)" "loopback 127.0.0.1"

# Nothing at all.
stub ipconfig 'exit 1'
check "falls back to loopback with nothing available" \
  "$(PATH="$WORK:$PATH" DROVER_OS=darwin detect_address)" "loopback 127.0.0.1"

# Linux path reads `ip -4 route get`.
rtk_ip_stub='if [ "$1" = "-4" ]; then
  echo "1.1.1.1 via 192.168.1.1 dev eth0 src 192.168.1.42 uid 1000"
fi'
stub ip "$rtk_ip_stub"
stub tailscale 'exit 1'
check "linux reads src from ip route get" \
  "$(PATH="$WORK:$PATH" DROVER_OS=linux detect_address)" "lan 192.168.1.42"

# A tailscale binary that is absent entirely must not error out.
rm -f "$WORK/tailscale"
check "absent tailscale is not an error" \
  "$(PATH="$WORK:$PATH" DROVER_OS=linux detect_address)" "lan 192.168.1.42"

# --- the CLI is often not on PATH -------------------------------------------
# Only Homebrew puts `tailscale` on PATH. The macOS app ships the CLI inside
# its bundle, so a PATH-only check silently gives tailnet users a LAN address.
# Observed on the primary dev machine: live tailnet, no tailscale on PATH.
FAKE_APP="$WORK/Applications/Tailscale.app/Contents/MacOS"
mkdir -p "$FAKE_APP"
cat > "$FAKE_APP/Tailscale" <<'STUB'
#!/usr/bin/env bash
case "$1" in
  status) echo "100.97.15.109  host  macOS  -" ;;
  ip)     echo "100.97.15.109" ;;
esac
STUB
chmod +x "$FAKE_APP/Tailscale"

check "finds the CLI when it is not on PATH" \
  "$(PATH="$WORK:$PATH" DROVER_TAILSCALE_CANDIDATES="$FAKE_APP/Tailscale" \
     DROVER_OS=darwin detect_address)" \
  "tailscale 100.97.15.109"

# An off-PATH CLI that is logged out still must not win.
cat > "$FAKE_APP/Tailscale" <<'STUB'
#!/usr/bin/env bash
exit 1
STUB
chmod +x "$FAKE_APP/Tailscale"
stub ipconfig 'echo "192.168.1.5"'
check "off-PATH but logged out falls back to LAN" \
  "$(PATH="$WORK:$PATH" DROVER_TAILSCALE_CANDIDATES="$FAKE_APP/Tailscale" \
     DROVER_OS=darwin detect_address)" \
  "lan 192.168.1.5"

[ "$FAILURES" -eq 0 ] || exit 1
echo "all checks passed"
