#!/usr/bin/env bash
# Keep a real pipe into bash's stdin, while reporting the installer's status.
# An early exit can close stdin before cat finishes on a small pipe buffer;
# whether cat then receives SIGPIPE must not change the asserted exit status.
run_installer() {
  # A runner may ignore SIGPIPE, in which case cat prints EPIPE and exits 1.
  # Bash cannot restore a signal ignored on entry. Reset it in the producer
  # before exec so an early reader exit is always 141, not a real input error.
  "$FIXTURE_PYTHON" -c '
import os
import signal
import sys

signal.signal(signal.SIGPIPE, signal.SIG_DFL)
os.execv("/bin/cat", ["/bin/cat", sys.argv[1]])
' "$REPO/install.sh" 2> "$INSTALL_LOG.input" \
    | /bin/bash -s -- "$@" > "$INSTALL_LOG" 2>&1
  local producer_status="${PIPESTATUS[0]}" result="${PIPESTATUS[1]}"
  if [ "$producer_status" -ne 0 ] && [ "$producer_status" -ne 141 ]; then
    /bin/cat "$INSTALL_LOG.input" >> "$INSTALL_LOG"
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
