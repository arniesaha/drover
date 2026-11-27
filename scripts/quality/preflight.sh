#!/bin/sh
# POSIX bootstrap for scripts/quality/preflight.py (contract:
# docs/operations/dev-preflight.md). Finds a Python >= 3.11 to run the
# stdlib-only helper; never assumes an unversioned `python`. The helper then
# separately verifies the *project* interpreter (default <root>/.venv).
# Exit 3 = environment_unavailable, 4 = harness_failure.
set -u
# Builtins only until an interpreter is found: PATH may be empty.
case $0 in */*) here=${0%/*} ;; *) here=. ;; esac
here=$(CDPATH='' cd -- "$here" && pwd -P) || exit 4
root=$(CDPATH='' cd -- "$here/../.." && pwd -P) || exit 4
for cand in "${DROVER_PREFLIGHT_PYTHON:-}" "$root/.venv/bin/python" \
    python3.14 python3.13 python3.12 python3.11 python3; do
  [ -n "$cand" ] || continue
  command -v "$cand" >/dev/null 2>&1 || continue
  if "$cand" -I -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' \
      >/dev/null 2>&1; then
    exec "$cand" -I "$here/preflight.py" --root "$root" "$@"
  fi
done
printf '%s\n' '{"check": "bootstrap_interpreter", "classification": "environment_unavailable"}'
printf '%s\n' "preflight environment_unavailable: [bootstrap_interpreter] no Python >= 3.11 found (tried DROVER_PREFLIGHT_PYTHON, $root/.venv/bin/python, python3.14..python3.11, python3). Install one, then: uv sync --locked --extra dev" >&2
exit 3
