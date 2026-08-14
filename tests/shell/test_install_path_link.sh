#!/usr/bin/env bash
# link_cli() puts drover-server on the user's PATH.
#
# Before this existed the installer finished and `drover-server` was still not
# a command: the whole runtime lives under ~/.drover/runtime, which is on
# nobody's PATH, and the closing pairing hint had to spell out the absolute
# path. The link is the difference between "installed" and "usable".
#
# The function is extracted and driven directly rather than running install.sh
# end to end, because the rest of the script downloads a real release. Each
# case gets its own fake home so they cannot leak into one another.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FAILURES=0

check_contains() {
  if printf '%s' "$2" | grep -q -- "$3"; then
    echo "ok   - $1"
  else
    echo "FAIL - $1 (missing '$3')"
    FAILURES=$((FAILURES + 1))
  fi
}
check_absent() {
  if printf '%s' "$2" | grep -q -- "$3"; then
    echo "FAIL - $1 (unexpectedly found '$3')"
    FAILURES=$((FAILURES + 1))
  else
    echo "ok   - $1"
  fi
}
check() {
  if [ "$2" = "$3" ]; then
    echo "ok   - $1"
  else
    echo "FAIL - $1 (got '$2', wanted '$3')"
    FAILURES=$((FAILURES + 1))
  fi
}

# Run link_cli in isolation with a given HOME and PATH. Everything the
# function needs is declared here, so the harness fails loudly if link_cli
# grows a new dependency rather than silently testing a stub.
run_link_cli() {
  local home="$1" path="$2"
  HOME="$home" PATH="$path" DROVER_HOME="$home/.drover" bash -c '
    RED=""; GREEN=""; YELLOW=""; CYAN=""; NC=""
    info()    { printf "%s\n" "$1"; }
    success() { printf "OK %s\n" "$1"; }
    warn()    { printf "WARN %s\n" "$1"; }
    fail()    { printf "FAIL %s\n" "$1" >&2; exit 1; }
    '"$(sed -n '/^link_cli() {$/,/^}$/p' "$REPO/install.sh")"'
    link_cli
  ' 2>&1
}

# A home whose path contains a space, matching the real dev checkout.
new_home() {
  local dir="$WORK/$1 dir"
  mkdir -p "$dir/.drover/runtime/current/bin"
  printf '#!/bin/sh\necho stub\n' > "$dir/.drover/runtime/current/bin/drover-server"
  chmod +x "$dir/.drover/runtime/current/bin/drover-server"
  printf '%s' "$dir"
}

echo "== the link is created and points through runtime/current =="
H="$(new_home basic)"
OUT="$(run_link_cli "$H" "$H/.local/bin:/usr/bin:/bin")"
check_contains "reports the link" "$OUT" "linked drover-server"
if [ -L "$H/.local/bin/drover-server" ]; then
  echo "ok   - a symlink was created"
else
  echo "FAIL - no symlink at ~/.local/bin/drover-server"
  FAILURES=$((FAILURES + 1))
fi
# Through `current`, never a version directory: a pinned path would keep
# resolving to the old build after an update, which is exactly how the
# service units made the symlink flip a no-op on every existing host.
check "points through runtime/current" \
  "$(readlink "$H/.local/bin/drover-server")" \
  "$H/.drover/runtime/current/bin/drover-server"
check_absent "no version directory in the link target" \
  "$(readlink "$H/.local/bin/drover-server")" "runtime/0."

echo
echo "== on PATH: no warning =="
check_absent "stays quiet when ~/.local/bin is on PATH" "$OUT" "not on your PATH"

echo
echo "== not on PATH: warns loudly and prints the fix =="
H="$(new_home nopath)"
OUT="$(run_link_cli "$H" "/usr/bin:/bin")"
check_contains "warns" "$OUT" "not on your PATH"
check_contains "prints the export line" "$OUT" 'export PATH='
check_contains "the export line names the real directory" "$OUT" "$H/.local/bin"

echo
echo "== idempotent: running twice is not an error =="
H="$(new_home twice)"
run_link_cli "$H" "$H/.local/bin:/usr/bin:/bin" >/dev/null
OUT="$(run_link_cli "$H" "$H/.local/bin:/usr/bin:/bin")"
check_contains "second run still reports success" "$OUT" "linked drover-server"
check "still a symlink after two runs" \
  "$(readlink "$H/.local/bin/drover-server")" \
  "$H/.drover/runtime/current/bin/drover-server"

echo
echo "== a stale link from an older install is replaced =="
H="$(new_home stale)"
mkdir -p "$H/.local/bin"
ln -sfn "$H/.drover/runtime/0.1.2/bin/drover-server" "$H/.local/bin/drover-server"
OUT="$(run_link_cli "$H" "$H/.local/bin:/usr/bin:/bin")"
check "a version-pinned link is repointed at current" \
  "$(readlink "$H/.local/bin/drover-server")" \
  "$H/.drover/runtime/current/bin/drover-server"

echo
echo "== someone else's binary is never clobbered =="
H="$(new_home occupied)"
mkdir -p "$H/.local/bin"
printf '#!/bin/sh\necho not ours\n' > "$H/.local/bin/drover-server"
chmod +x "$H/.local/bin/drover-server"
OUT="$(run_link_cli "$H" "$H/.local/bin:/usr/bin:/bin")"
check_contains "says it left the file alone" "$OUT" "left alone"
check_absent "does not claim to have linked" "$OUT" "linked drover-server"
check "the existing file is untouched" \
  "$(cat "$H/.local/bin/drover-server")" \
  "$(printf '#!/bin/sh\necho not ours')"
if [ -L "$H/.local/bin/drover-server" ]; then
  echo "FAIL - the real file was replaced by a symlink"
  FAILURES=$((FAILURES + 1))
else
  echo "ok   - still a regular file"
fi

echo
if [ "$FAILURES" -eq 0 ]; then
  echo "all install PATH-link checks passed"
else
  echo "$FAILURES check(s) failed"
fi
exit "$FAILURES"
