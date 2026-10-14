#!/usr/bin/env bash
# Keep a real pipe into bash's stdin, while reporting the installer's status.
# An early exit can close stdin before cat finishes on a small pipe buffer;
# whether cat then receives SIGPIPE must not change the asserted exit status.
run_installer() {
  /bin/cat "$REPO/install.sh" | /bin/bash -s -- "$@" > "$INSTALL_LOG" 2>&1
  local producer_status="${PIPESTATUS[0]}" result="${PIPESTATUS[1]}"
  if [ "$producer_status" -ne 0 ] && [ "$producer_status" -ne 141 ]; then
    printf 'installer input failed (exit %s)\n' "$producer_status" >> "$INSTALL_LOG"
    result="$producer_status"
  fi
  /bin/cat "$INSTALL_LOG"
  return "$result"
}

run_checkout_installer() {
  /bin/bash "$REPO/install.sh" "$@" > "$INSTALL_LOG" 2>&1
  local result=$?
  /bin/cat "$INSTALL_LOG"
  return "$result"
}
