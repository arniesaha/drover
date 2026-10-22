#!/usr/bin/env bash
# Run a UAT-only harness process with one private, throwaway picker root.
# This deliberately affects only the child command's environment. It does not
# edit launchd/systemd configuration or widen a running production daemon.
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/uat-run.sh [--root PATH] -- COMMAND [ARG ...]

Creates a private UAT directory (default: "$HOME/drover-uat") and runs COMMAND
with DROVER_FOLDER_ROOTS set to that directory only. PATH must be an absolute,
non-symlinked descendant of HOME. This is for an isolated UAT harness process;
it does not change a running host service.
USAGE
}

root="${DROVER_UAT_ROOT:-$HOME/drover-uat}"
if [[ "${1:-}" == "--root" ]]; then
  [[ $# -ge 2 ]] || { usage >&2; exit 2; }
  root="$2"
  shift 2
fi
[[ "${1:-}" == "--" ]] || { usage >&2; exit 2; }
shift
[[ $# -gt 0 ]] || { usage >&2; exit 2; }

# Keep the UAT root narrowly scoped to the daemon account's home. The daemon
# independently rejects symlinks and traversal when it opens configured roots.
case "$root" in
  "$HOME"/*) ;;
  *) echo "UAT root must be an absolute descendant of HOME; refusing: $root" >&2; exit 2 ;;
esac
if [[ "$root" == *'/../'* || "$root" == */.. || "$root" == *'/./'* || "$root" == */. ]]; then
  echo "UAT root must not contain traversal components; refusing: $root" >&2
  exit 2
fi
# Reject every existing component below HOME, not just the final path.  Otherwise
# `~/linked/drover-uat` could make mkdir traverse a symlink outside the private
# UAT tree before the final-root check runs.
relative_root="${root#"$HOME"/}"
current="$HOME"
IFS=/ read -r -a components <<< "$relative_root"
for component in "${components[@]}"; do
  current="$current/$component"
  if [[ -L "$current" ]]; then
    echo "UAT root must not contain symlink components; refusing: $root" >&2
    exit 2
  fi
done

umask 077
if [[ ! -d "$root" ]]; then
  mkdir -p -m 700 "$root"
fi
[[ -d "$root" && ! -L "$root" ]] || {
  echo "UAT root is not a directory; refusing: $root" >&2
  exit 2
}
chmod 700 "$root"
exec env DROVER_FOLDER_ROOTS="$root" "$@"
