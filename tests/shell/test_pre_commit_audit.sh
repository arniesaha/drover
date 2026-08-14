#!/usr/bin/env bash
# The public release audit only runs in CI, which is after the push: by then a
# private planning document is already public and main is red for everybody
# except the author. The pre-commit hook runs the same audit over the staged
# set so the author is the one who finds out. Modelled on test_verify.sh.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# Quote every path: this repository is routinely checked out under a
# directory containing a space, and an unquoted path fails in ways that look
# like a product bug.
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
HOOK="$REPO_ROOT/.githooks/pre-commit"
AUDIT="$REPO_ROOT/scripts/check_public_release.py"

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

if [ ! -x "$HOOK" ]; then
  echo "FAIL - .githooks/pre-commit exists and is executable"
  exit 1
fi

# Every case gets its own throwaway repository so one failure cannot leak
# state into the next. The seed commit bypasses the hook: it installs the
# hook, so there is nothing to audit yet.
new_repo() {
  local repo="$WORK/$1"
  mkdir -p "$repo/scripts" "$repo/.githooks" "$repo/docs"
  cp "$AUDIT" "$repo/scripts/check_public_release.py"
  cp "$HOOK" "$repo/.githooks/pre-commit"
  chmod +x "$repo/.githooks/pre-commit"
  git -C "$repo" init -q -b main
  git -C "$repo" config user.email "test@drover.local"
  git -C "$repo" config user.name "Drover Test"
  git -C "$repo" config commit.gpgsign false
  git -C "$repo" config core.hooksPath .githooks
  printf 'Drover keeps agent sessions local.\n' > "$repo/docs/overview.md"
  git -C "$repo" add -A
  git -C "$repo" commit -q --no-verify -m "seed"
  printf '%s' "$repo"
}

# A change that breaks no rule must commit exactly as it did before.
REPO="$(new_repo clean)"
printf 'Add a host with the printed one-liner.\n' > "$REPO/docs/multi-host.md"
git -C "$REPO" add docs/multi-host.md
git -C "$REPO" commit -q -m "docs: add multi-host note" > "$WORK/clean.log" 2>&1
check "a clean staged change commits" "$?" "0"

# The file this whole change exists for: brand new, never tracked, and the
# author has no signal. Running the audit by hand does not give them one,
# because it walks `git ls-files` and an unstaged file is not in the index.
REPO="$(new_repo planning)"
mkdir -p "$REPO/docs/superpowers"
printf '# Phase 1\n\nImplement the thing.\n' > "$REPO/docs/superpowers/plan.md"
(cd "$REPO" && python3 scripts/check_public_release.py > "$WORK/unstaged-audit.log" 2>&1)
check "the audit by hand clears a new file that is not staged yet" "$?" "0"
git -C "$REPO" add docs/superpowers/plan.md
git -C "$REPO" commit -q -m "docs: add plan" > "$WORK/planning.log" 2>&1
check "a newly added planning document is rejected" "$?" "1"
check "the rejection names the offending path and the rule" \
  "$(grep -c 'docs/superpowers/plan.md:1: private-planning-path' "$WORK/planning.log")" "1"
check "the rejection says how to bypass it deliberately" \
  "$(grep -c -- '--no-verify' "$WORK/planning.log")" "1"
check "the rejected commit did not land" \
  "$(git -C "$REPO" rev-list --count HEAD)" "1"

# A deliberate bypass has to keep working, or the hook becomes a wall.
git -C "$REPO" commit -q --no-verify -m "docs: add plan" > "$WORK/bypass.log" 2>&1
check "--no-verify bypasses the hook" "$?" "0"

# docs/roadmap.md is the other private-planning path.
REPO="$(new_repo roadmap)"
printf '# Roadmap\n\nNext quarter.\n' > "$REPO/docs/roadmap.md"
git -C "$REPO" add docs/roadmap.md
git -C "$REPO" commit -q -m "docs: roadmap" > "$WORK/roadmap.log" 2>&1
check "docs/roadmap.md is rejected" "$?" "1"

# Path rules are the cheap half. Content rules have to run too, and against a
# file that was already tracked.
REPO="$(new_repo content)"
printf 'Run it from /Users/alice/projects/drover.\n' > "$REPO/docs/overview.md"
git -C "$REPO" add docs/overview.md
git -C "$REPO" commit -q -m "docs: expand overview" > "$WORK/content.log" 2>&1
check "a modification that leaks a private value is rejected" "$?" "1"
check "the content rejection names the rule" \
  "$(grep -c 'personal-home-path' "$WORK/content.log")" "1"

# What gets committed is the index, not the working tree. Auditing the working
# tree would clear a commit that is about to publish the violation.
REPO="$(new_repo staged-content)"
printf 'Run it from /Users/alice/projects/drover.\n' > "$REPO/docs/overview.md"
git -C "$REPO" add docs/overview.md
printf 'Run it from your checkout.\n' > "$REPO/docs/overview.md"
git -C "$REPO" commit -q -m "docs: expand overview" > "$WORK/staged-content.log" 2>&1
check "the staged content is audited, not the working tree" "$?" "1"

# The mirror image: an unstaged violation is not being committed, so it must
# not block work that is.
REPO="$(new_repo unstaged)"
printf 'Add a host with the printed one-liner.\n' > "$REPO/docs/multi-host.md"
git -C "$REPO" add docs/multi-host.md
printf 'Scratch note about /Users/alice/projects/drover.\n' > "$REPO/docs/overview.md"
git -C "$REPO" commit -q -m "docs: add multi-host note" > "$WORK/unstaged.log" 2>&1
check "an unstaged violation does not block the commit" "$?" "0"

# The audit is scoped to the change. A finding that is already committed is
# somebody else's problem to fix, not a wall across every later commit.
REPO="$(new_repo pre-existing)"
printf 'Run it from /Users/alice/projects/drover.\n' > "$REPO/docs/overview.md"
git -C "$REPO" add docs/overview.md
git -C "$REPO" commit -q --no-verify -m "docs: expand overview"
printf 'Add a host with the printed one-liner.\n' > "$REPO/docs/multi-host.md"
git -C "$REPO" add docs/multi-host.md
git -C "$REPO" commit -q -m "docs: add multi-host note" > "$WORK/pre-existing.log" 2>&1
check "an already committed finding does not block an unrelated commit" "$?" "0"

# Deletions carry no content and must not be looked up on disk.
REPO="$(new_repo deletion)"
git -C "$REPO" rm -q docs/overview.md
git -C "$REPO" commit -q -m "docs: drop overview" > "$WORK/deletion.log" 2>&1
check "a staged deletion commits" "$?" "0"

# This repository is routinely checked out under a directory with a space in
# it, and staged paths can contain spaces too.
REPO="$(new_repo "dir with space")"
printf 'A note.\n' > "$REPO/docs/release notes.md"
git -C "$REPO" add "docs/release notes.md"
git -C "$REPO" commit -q -m "docs: add release notes" > "$WORK/space.log" 2>&1
check "a staged path containing a space commits" "$?" "0"
mkdir -p "$REPO/docs/superpowers"
printf '# Plan\n' > "$REPO/docs/superpowers/my plan.md"
git -C "$REPO" add "docs/superpowers/my plan.md"
git -C "$REPO" commit -q -m "docs: add plan" > "$WORK/space-bad.log" 2>&1
check "a violating path containing a space is rejected" "$?" "1"

# An empty commit stages nothing, so there is nothing to audit.
REPO="$(new_repo empty)"
git -C "$REPO" commit -q --allow-empty -m "chore: empty" > "$WORK/empty.log" 2>&1
check "an empty commit is allowed" "$?" "0"

# A branch without the audit script cannot be held to it.
REPO="$(new_repo no-audit)"
git -C "$REPO" rm -q scripts/check_public_release.py
git -C "$REPO" commit -q -m "chore: drop audit" > "$WORK/no-audit.log" 2>&1
check "a repository without the audit script commits" "$?" "0"

[ "$FAILURES" -eq 0 ] || exit 1
echo "all checks passed"
