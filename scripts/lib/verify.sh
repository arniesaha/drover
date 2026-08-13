#!/usr/bin/env bash
# Checksum helpers shared by install.sh and the updater.
#
# Every failure path here refuses. There is no "warn and continue": the whole
# point of the manifest is that an artifact we cannot prove is an artifact we
# do not run. Sourced, not executed, so nothing here has side effects.

sha256_digest() {
  local file="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$file" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$file" | awk '{print $1}'
  else
    echo "no SHA-256 tool found (install sha256sum or shasum)" >&2
    return 1
  fi
}

verify_against_manifest() {
  local file="$1" name="$2" manifest="$3" expected actual

  if [ ! -s "$manifest" ]; then
    echo "checksum manifest is missing or empty: $manifest" >&2
    return 1
  fi

  # Match the whole field, so "artifact.whl" never matches an entry for
  # "other-artifact.whl". Accepts plain, binary-mode (*name), and ./name
  # spellings, which is the range GNU and BSD tools emit.
  expected="$(awk -v want="$name" \
    '($2 == want || $2 == "*" want || $2 == "./" want) { print $1; exit }' \
    "$manifest")"
  if [ -z "$expected" ]; then
    echo "no checksum entry for $name -- refusing to install" >&2
    return 1
  fi

  expected="$(printf '%s' "$expected" | tr '[:upper:]' '[:lower:]')"
  if ! printf '%s\n' "$expected" | grep -Eq '^[0-9a-f]{64}$'; then
    echo "invalid checksum entry for $name -- refusing to install" >&2
    return 1
  fi

  actual="$(sha256_digest "$file" | tr '[:upper:]' '[:lower:]')" || return 1
  if [ "$actual" != "$expected" ]; then
    echo "checksum mismatch for $name -- refusing to install" >&2
    return 1
  fi
  return 0
}
