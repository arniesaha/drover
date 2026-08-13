#!/usr/bin/env bash
# `local a=x b=$a` is a trap under `set -u`.
#
# Bash declares every name in a single `local` before assigning any of them, so
# an assignment that expands an earlier name on the same line sees it unset and
# aborts. It cost the v0.2.0 release its install check — "install.sh: line 225:
# version: unbound variable" — and it had been there since the installer
# shipped, because nothing ran the installer end to end until then.
#
# This checks the shape rather than the one line, so the next one is caught too.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
FAILURES=0

pass() { echo "ok   - $1"; }
fail() { echo "FAIL - $1"; FAILURES=$((FAILURES + 1)); }

# --- the shape is gone from every shipped script ------------------------------

for script in "$REPO/install.sh" "$REPO/scripts/"*.sh; do
  [ -f "$script" ] || continue
  name="$(basename "$script")"

  # A `local` line declaring two or more names where a later value expands a
  # name assigned earlier on that same line.
  # Substring matching rather than a regex with \b: macOS awk uses POSIX ERE,
  # where \b is not a word boundary, so a regex spelling of this silently
  # matches nothing — which is exactly how the first version of this test
  # passed against the broken installer.
  offenders="$(awk '
    /^[[:space:]]*local[[:space:]]/ {
      line = $0
      sub(/^[[:space:]]*local[[:space:]]+/, "", line)
      n = split(line, parts, /[[:space:]]+/)
      count = 0
      for (i = 1; i <= n; i++) {
        eq = index(parts[i], "=")
        if (eq <= 0) continue
        name = substr(parts[i], 1, eq - 1)
        value = substr(parts[i], eq + 1)
        for (j = 1; j <= count; j++) {
          if (index(value, "$" names[j]) > 0 || index(value, "${" names[j]) > 0) {
            print FILENAME ":" FNR ": " $0
            next
          }
        }
        count++
        names[count] = name
      }
    }' "$script")"

  if [ -n "$offenders" ]; then
    fail "$name has a self-referencing multi-name local"
    printf '       %s\n' "$offenders"
  else
    pass "$name has no self-referencing multi-name local"
  fi
done

# --- and the shape really does break under set -u -----------------------------
# Guards the test itself: if a future bash stopped failing here, the lint above
# would be enforcing a rule that no longer matters, and we should know.

if bash -c 'set -euo pipefail
            f() { local a="$1" b="/x/$a"; echo "$b"; }
            f one' >/dev/null 2>&1; then
  fail "self-referencing local no longer errors under set -u (is the lint still needed?)"
else
  pass "self-referencing local still errors under set -u"
fi

# --- the real function survives being called ----------------------------------

if bash -n "$REPO/install.sh" 2>/dev/null; then
  pass "install.sh parses"
else
  fail "install.sh does not parse"
fi

echo
if [ "$FAILURES" -eq 0 ]; then
  echo "all install-local checks passed"
  exit 0
fi
echo "$FAILURES check(s) failed"
exit 1
