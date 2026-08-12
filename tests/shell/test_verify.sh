#!/usr/bin/env bash
# Checksum verification must refuse anything it cannot prove. Modelled on
# mobilecli/scripts/test-installer-checksum.sh.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# Quote every path: this repository is routinely checked out under a
# directory containing a space, and an unquoted path fails in ways that look
# like a product bug.
source "$HERE/../../scripts/lib/verify.sh"

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

printf 'payload' > "$WORK/artifact.whl"
DIGEST="$(sha256_digest "$WORK/artifact.whl")"
check "digest is 64 lowercase hex" \
  "$(printf '%s' "$DIGEST" | grep -cE '^[0-9a-f]{64}$')" "1"

printf '%s  artifact.whl\n' "$DIGEST" > "$WORK/SHA256SUMS.txt"
verify_against_manifest "$WORK/artifact.whl" "artifact.whl" "$WORK/SHA256SUMS.txt"
check "a matching digest verifies" "$?" "0"

printf 'tampered' > "$WORK/artifact.whl"
verify_against_manifest "$WORK/artifact.whl" "artifact.whl" "$WORK/SHA256SUMS.txt" 2>/dev/null
check "a tampered artifact is refused" "$?" "1"

printf 'payload' > "$WORK/artifact.whl"
printf '%s  other.whl\n' "$DIGEST" > "$WORK/missing.txt"
verify_against_manifest "$WORK/artifact.whl" "artifact.whl" "$WORK/missing.txt" 2>/dev/null
check "a missing manifest entry is refused" "$?" "1"

: > "$WORK/empty.txt"
verify_against_manifest "$WORK/artifact.whl" "artifact.whl" "$WORK/empty.txt" 2>/dev/null
check "an empty manifest is refused" "$?" "1"

verify_against_manifest "$WORK/artifact.whl" "artifact.whl" "$WORK/nope.txt" 2>/dev/null
check "an absent manifest is refused" "$?" "1"

printf 'notahexdigest  artifact.whl\n' > "$WORK/junk.txt"
verify_against_manifest "$WORK/artifact.whl" "artifact.whl" "$WORK/junk.txt" 2>/dev/null
check "a malformed digest is refused" "$?" "1"

printf '%s  *artifact.whl\n' "$DIGEST" > "$WORK/star.txt"
verify_against_manifest "$WORK/artifact.whl" "artifact.whl" "$WORK/star.txt"
check "binary-mode manifest entries are accepted" "$?" "0"

printf '%s  ./artifact.whl\n' "$DIGEST" > "$WORK/dot.txt"
verify_against_manifest "$WORK/artifact.whl" "artifact.whl" "$WORK/dot.txt"
check "./-prefixed manifest entries are accepted" "$?" "0"

# Uppercase digests are valid sha256 output from some tools.
printf '%s  artifact.whl\n' "$(printf '%s' "$DIGEST" | tr '[:lower:]' '[:upper:]')" \
  > "$WORK/upper.txt"
verify_against_manifest "$WORK/artifact.whl" "artifact.whl" "$WORK/upper.txt"
check "uppercase manifest digests verify" "$?" "0"

# A real manifest lists several files; the right entry must be picked.
printf 'a%.0s' $(seq 64) > /dev/null
{
  printf '%s  requirements.lock.txt\n' "$(printf 'b%.0s' $(seq 64))"
  printf '%s  artifact.whl\n' "$DIGEST"
} > "$WORK/multi.txt"
verify_against_manifest "$WORK/artifact.whl" "artifact.whl" "$WORK/multi.txt"
check "the right entry is picked from a multi-file manifest" "$?" "0"

# A name that is a suffix of another must not match by accident.
{
  printf '%s  other-artifact.whl\n' "$DIGEST"
} > "$WORK/suffix.txt"
verify_against_manifest "$WORK/artifact.whl" "artifact.whl" "$WORK/suffix.txt" 2>/dev/null
check "a suffix name does not match" "$?" "1"

# Paths containing spaces must survive.
mkdir -p "$WORK/dir with space"
printf 'payload' > "$WORK/dir with space/artifact.whl"
printf '%s  artifact.whl\n' "$DIGEST" > "$WORK/dir with space/SHA256SUMS.txt"
verify_against_manifest "$WORK/dir with space/artifact.whl" "artifact.whl" \
  "$WORK/dir with space/SHA256SUMS.txt"
check "a path containing a space verifies" "$?" "0"

[ "$FAILURES" -eq 0 ] || exit 1
echo "all checks passed"
