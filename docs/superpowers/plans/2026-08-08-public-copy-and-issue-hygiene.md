# Public Copy and Issue Hygiene Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Drover a direct, consistent public voice, remove em dashes and irrelevant legacy-product positioning from current public prose, and reconcile every currently open issue with verified repository or runtime state.

**Architecture:** Add two objective, path-scoped checks to the existing public-release scanner, then edit only the public prose surface defined in the approved design. Merge tracked changes through protected `main` before changing repository metadata or issue records. Snapshot every external record privately, apply evidence-based edits, and read the resulting GitHub state back exactly.

**Tech Stack:** Python 3.11, pytest, Markdown, Git, GitHub CLI, GitHub Actions, Swift Package Manager

## Global Constraints

- Work in `/Volumes/M2 1/drover/.worktrees/public-copy-hygiene` on branch `docs/public-copy-hygiene` until the pull request is merged.
- Preserve source comments, doc comments, UI strings, error strings, runtime prompts, tests, fixtures, snapshots, and captured protocol data unless a test must be added for the new scanner behavior.
- Retain `nexus.*` only where it names an actual compatibility contract. New public interfaces and current product positioning use Drover.
- Replace each in-scope em dash according to meaning. Do not mechanically replace it with a hyphen.
- Do not use a tone blacklist. Objective checks cover only Unicode em dashes and “formerly Nexus” positioning in defined public-prose paths.
- Do not expose private paths, endpoints, credentials, runner tokens, or private audit backups in commits, pull requests, issue edits, or reports.
- Do not close an issue without a commit, pull request, test, or live-runtime result that proves resolution or supersession.
- Issue #50 remains open until the self-hosted runner has completed its allowed-job, rejected-PR, cleanup, and restart proofs.
- Run `git diff --check` and inspect the exact staged diff before every commit.

---

## Task 1: Add public-copy regression tests

**Files:**

- Modify: `tests/test_check_public_release.py`
- Test: `tests/test_check_public_release.py`

- [ ] **Step 1: Add failing tests for the two objective rules**

Append tests that prove an em dash and legacy positioning are rejected in public Markdown:

```python
def test_check_paths_rejects_em_dash_in_public_prose(tmp_path: Path) -> None:
    path = write_file(
        tmp_path,
        "docs/overview.md",
        "Drover is local-first — sessions stay under your control.\n",
    )

    findings = check_paths([path])

    assert [finding.rule for finding in findings] == ["public-em-dash"]


def test_check_paths_rejects_legacy_product_positioning(tmp_path: Path) -> None:
    path = write_file(
        tmp_path,
        "README.md",
        "Drover, formerly Nexus, manages coding-agent sessions.\n",
    )

    findings = check_paths([path])

    assert [finding.rule for finding in findings] == [
        "legacy-positioning-copy"
    ]
```

- [ ] **Step 2: Add failing scope-boundary tests**

Add tests proving that source comments, runtime prompts, tests, and fixtures are outside the punctuation rule, while a technical compatibility name remains allowed:

```python
def test_check_paths_limits_copy_rules_to_public_prose(tmp_path: Path) -> None:
    paths = [
        write_file(tmp_path, "src/drover/client.py", "# retry — then fail\n"),
        write_file(tmp_path, "src/drover/prompts/system.md", "Think — then act.\n"),
        write_file(tmp_path, "tests/fixtures/session.md", "Captured — unchanged.\n"),
    ]

    assert check_paths(paths) == []


def test_check_paths_allows_nexus_compatibility_contract(tmp_path: Path) -> None:
    path = write_file(
        tmp_path,
        "docs/compatibility.md",
        "The `nexus.*` telemetry keys remain readable for compatibility.\n",
    )

    assert check_paths([path]) == []
```

- [ ] **Step 3: Run the focused tests and confirm the intended failure**

Run:

```bash
uv run pytest -q tests/test_check_public_release.py
```

Expected: the new positive tests fail because `public-em-dash` and `legacy-positioning-copy` do not exist yet. The exclusion and compatibility tests should already pass.

- [ ] **Step 4: Review the test diff**

Run:

```bash
git diff --check
git diff -- tests/test_check_public_release.py
```

Confirm that the fixtures contain no real secret or private environment value.

- [ ] **Step 5: Commit the failing tests**

```bash
git add tests/test_check_public_release.py
git commit -m "test: define public copy release checks"
```

---

## Task 2: Implement path-scoped public-copy checks

**Files:**

- Modify: `scripts/check_public_release.py`
- Test: `tests/test_check_public_release.py`

- [ ] **Step 1: Define the public-prose path boundary**

Add a helper near `_is_release_facing`:

```python
def _is_public_prose_path(path: Path) -> bool:
    parts = path.parts
    normalized = path.as_posix()
    if path.suffix.lower() != ".md":
        return False
    if "tests" in parts or "fixtures" in parts or "snapshots" in parts:
        return False
    if "/src/drover/prompts/" in f"/{normalized.lstrip('/')}":
        return False
    if path.name.lower() == "readme.md":
        return True
    return "docs" in parts or "skills" in parts
```

This deliberately includes the root README, `apps/drover/README.md`, Markdown under `docs/`, and contributor-facing Markdown under `skills/`. It excludes behavior-bearing prompts and captured test material.

- [ ] **Step 2: Define the legacy-positioning pattern**

Add a case-insensitive pattern that requires both the positioning word and the prior name:

```python
LEGACY_POSITIONING_PATTERN = re.compile(
    r"\bformerly\b[^\r\n]{0,80}\bNexus\b",
    re.IGNORECASE,
)
```

Do not match `nexus.*`, compatibility prose, historical API identifiers, or a bare occurrence of `Nexus`.

- [ ] **Step 3: Emit the two findings only for public prose**

Inside the line loop in `check_paths`, after the credential check and before the general rule loop, add:

```python
if _is_public_prose_path(path):
    if "—" in line:
        findings.append(
            Finding(
                path=str(path),
                line=line_number,
                rule="public-em-dash",
                excerpt=line.strip(),
            )
        )
    if LEGACY_POSITIONING_PATTERN.search(line):
        findings.append(
            Finding(
                path=str(path),
                line=line_number,
                rule="legacy-positioning-copy",
                excerpt=line.strip(),
            )
        )
```

- [ ] **Step 4: Run the focused tests**

Run:

```bash
uv run pytest -q tests/test_check_public_release.py
```

Expected: all scanner tests pass.

- [ ] **Step 5: Run the scanner against the current tracked tree**

Run:

```bash
.venv/bin/python scripts/check_public_release.py
```

Expected at this stage: findings may identify in-scope copy that Task 3 will edit, plus the known private planning paths that are removed before the pull request.

- [ ] **Step 6: Review and commit the implementation**

```bash
git diff --check
git diff -- scripts/check_public_release.py tests/test_check_public_release.py
git add scripts/check_public_release.py
git commit -m "feat: check public copy in release audit"
```

---

## Task 3: Edit tracked public prose

**Files:**

- Modify as needed: `README.md`
- Modify as needed: `docs/*.md`
- Modify as needed: `apps/drover/README.md`
- Modify as needed: `skills/**/*.md`
- Specifically inspect: `docs/security.md`
- Specifically inspect: `skills/drover/references/drover-agentweave-langfuse-positioning.md`

- [ ] **Step 1: Produce the scoped inventory**

Run:

```bash
git ls-files '*.md'
rg -n '—|[Ff]ormerly.{0,80}[Nn]exus|\b(powerful|robust|seamless)\b|not (just|only).*but' README.md docs apps/drover/README.md skills
```

Classify every match as editable public prose, required technical compatibility, or out of scope. Record classifications in the private task report, not in a committed audit artifact.

- [ ] **Step 2: Apply the editorial rules line by line**

Make the smallest edits that improve the current public surface:

- Use sentence-case headings in the README where doing so improves consistency.
- Keep the primary tagline once; remove repeated slogans or broad claims.
- Replace vague capability words with the concrete capability in `docs/security.md`.
- Rewrite `retrieval—not as a second dashboard` in the positioning guide as a direct sentence without an em dash.
- Remove current marketing language that explains Drover as an earlier product.
- Preserve `nexus.*`, `NexusKit`, or stored-name references only when the paragraph clearly describes compatibility.
- Leave already direct, accurate documents unchanged.

- [ ] **Step 3: Run objective prose checks**

Run:

```bash
.venv/bin/python scripts/check_public_release.py
rg -n '—|[Ff]ormerly.{0,80}[Nn]exus' README.md docs apps/drover/README.md skills
```

Expected: no public-copy findings. The scanner may still report only the intentionally temporary `docs/superpowers/` planning paths.

- [ ] **Step 4: Check changed local Markdown links**

The current tree has no dedicated Markdown link checker. Enumerate every added or changed link in the branch:

```bash
git diff --unified=0 origin/main...HEAD -- '*.md' | rg '^\+.*\]\([^)]+\)'
```

For each local target, remove any anchor and verify the resulting path with `test -e` relative to the document that owns the link. Open each changed HTTP link and require a successful response or a documented intentional redirect. If the inventory is empty, record that no links changed.

- [ ] **Step 5: Review the prose diff for changed meaning**

Run:

```bash
git diff --check
git diff --word-diff=plain -- README.md docs apps/drover/README.md skills
```

Confirm each changed sentence remains technically true and no compatibility identifier changed.

- [ ] **Step 6: Commit the editorial pass**

```bash
git add README.md docs apps/drover/README.md skills
git commit -m "docs: tighten Drover public copy"
```

---

## Task 4: Remove private planning files and verify the branch

**Files:**

- Delete before pull request: `docs/superpowers/specs/2026-08-08-public-copy-and-issue-hygiene-design.md`
- Delete before pull request: `docs/superpowers/plans/2026-08-08-public-copy-and-issue-hygiene.md`

- [ ] **Step 1: Preserve the plan in Git history, then remove the visible planning tree**

Run:

```bash
git rm -r docs/superpowers
git diff --check
git commit -m "chore: remove private planning artifacts"
```

The design and plan remain recoverable from commits `936c668` and the plan commit, but they must not ship in the public tree.

- [ ] **Step 2: Run the release scanner and focused tests**

Run:

```bash
.venv/bin/python scripts/check_public_release.py
uv run pytest -q tests/test_check_public_release.py
```

Expected: zero scanner findings and all focused tests pass.

- [ ] **Step 3: Run proportional repository verification**

Run:

```bash
uv run black --check .
uv run pytest -q
swift test --package-path apps/drover/DroverKit
git diff --check origin/main...HEAD
git status --short
```

Expected: formatting, Python, and Swift tests pass; the branch has no uncommitted changes.

- [ ] **Step 4: Inspect the complete branch diff**

Run:

```bash
git diff --stat origin/main...HEAD
git diff origin/main...HEAD
```

Confirm that the visible branch contains only scanner tests, scanner implementation, and scoped public-copy edits.

---

## Task 5: Open, review, and merge the protected pull request

**Files:**

- External: GitHub pull request for `docs/public-copy-hygiene`

- [ ] **Step 1: Push the branch and open the pull request**

```bash
git push -u origin docs/public-copy-hygiene
gh pr create --base main --head docs/public-copy-hygiene --title "Tighten public copy and add issue hygiene checks" --body-file /private/tmp/drover-public-copy-pr.md
```

Create `/private/tmp/drover-public-copy-pr.md` with `apply_patch`. The body must summarize the scoped prose edits, the two new objective scanner rules, compatibility-name preservation, and the exact local verification results. It must contain no em dash or legacy positioning copy.

- [ ] **Step 2: Read the pull request back and inspect the rendered diff**

```bash
gh pr view --json number,title,body,baseRefName,headRefName,isDraft,url
gh pr diff
```

Check the PR body for the same public-copy rules and verify every changed prose line in the full diff.

- [ ] **Step 3: Wait for protected checks**

```bash
gh pr checks --watch
```

Required checks: `build-and-test` and `Build and test iOS app`. Investigate any failure before merging.

- [ ] **Step 4: Request independent review and address findings**

Use `superpowers:requesting-code-review`. Re-run the focused scanner tests and public-release audit after any edit.

- [ ] **Step 5: Merge through the protected branch**

```bash
gh pr merge --merge --delete-branch
gh pr view --json state,mergedAt,mergeCommit,url
```

Expected: the PR is `MERGED` and the merge commit is on remote `main`.

- [ ] **Step 6: Synchronize the primary local checkout without discarding local-only work**

In `/Volumes/M2 1/drover`, inspect status and ancestry first. Merge `origin/main` into local `main` with a normal non-destructive merge if local-only commits remain. Do not reset or force-push.

---

## Task 6: Update and verify repository metadata

**Files:**

- External: `arniesaha/drover` repository metadata
- Private backup: git-ignored task workspace with mode `0600`

- [ ] **Step 1: Snapshot current metadata privately**

Use `gh repo view arniesaha/drover --json nameWithOwner,description,homepageUrl,repositoryTopics,visibility` and save the exact JSON under the existing git-ignored task-report directory. Set the file mode to `0600`. Confirm `git status --short` does not expose the backup.

- [ ] **Step 2: Set the approved description**

```bash
gh repo edit arniesaha/drover --description "Drive your coding-agent fleet from your pocket. A local-first cockpit and context store for CLI coding agents."
```

Do not invent a homepage or topics. Change them only if live read-back proves an existing value is inaccurate and the correction is already documented in the repository.

- [ ] **Step 3: Read metadata back exactly**

```bash
gh repo view arniesaha/drover --json description,homepageUrl,repositoryTopics,visibility
```

Expected description:

```text
Drive your coding-agent fleet from your pocket. A local-first cockpit and context store for CLI coding agents.
```

Confirm the repository remains public and the description contains no em dash or legacy positioning.

---

## Task 7: Reconcile all 15 issues open at design time

**Files:**

- External: GitHub issues `#5`, `#11` through `#18`, `#20`, `#23`, `#24`, `#40`, `#41`, and `#50`
- Private backup: git-ignored task workspace with mode `0600`

- [ ] **Step 1: Snapshot every issue before editing**

Fetch each issue with title, body, state, labels, comments, URL, and timestamps. Save the 15-record JSON snapshot privately with mode `0600`. Verify the count is exactly 15 and the backup is not tracked.

- [ ] **Step 2: Reverify the classification evidence against merged `main` and live state**

Use this decision matrix. If current evidence contradicts a row, stop that issue edit and document the discrepancy rather than forcing the planned classification.

| Issue | Planned state | Required current title or action | Evidence gate |
| --- | --- | --- | --- |
| #5 | Open, operational follow-up | `Remove rollback artifacts after the Drover cutover soak` | Keep open until artifact deletion is proven; remove host/path inventory and old-name positioning. |
| #11 | Open, bug | Keep boot-race scope; correct the typo and use generic deployment terms | No verified fix currently proves clean boot ordering. |
| #12 | Open, enhancement | Replace `NexusKit` with `DroverKit`; state that one live interactive sign-in check remains | Merged implementation exists, but the named device validation is incomplete. |
| #13 | Open, enhancement | `Add per-host credentials before supporting public relay ingress` | Public ingress is unsupported; credentials are a future security prerequisite, not a live Funnel claim. |
| #14 | Close as superseded | Add evidence comment linking issue #47 and PR #49 | Public cutover is complete; new APIs use Drover while `nexus.*` storage and telemetry contracts remain compatible by design. |
| #15 | Open, enhancement | `Assess OKF v0.1 for the context layer` | Keep the artifact matrix and adopt/adapt/skip acceptance criteria. |
| #16 | Open, enhancement | `Move NAS harnessd to relay mode` | Operational follow-up remains and depends on #11. |
| #17 | Open, enhancement | `Add server-side token and cost rollups for session accounting` | Accounting prerequisite remains; retain concrete acceptance criteria. |
| #18 | Open, bug | State the stale host-picker reproduction and acceptance criteria directly | No verified fix currently closes the picker bug. |
| #20 | Open, bug | Keep the BGAppRefresh limitation and concrete options | Platform limitation remains; remove formulaic copy and em dashes. |
| #23 | Open, bug | Keep the Gemini image-attachment workspace reproduction and options | No verified fix currently closes the attachment bug. |
| #24 | Open, enhancement | Keep the physical-device ATX heading-render validation | Human device validation remains. |
| #40 | Open, enhancement | `Prepare CI, module boundaries, and framework decisions for Loop Engine work` | Preserve still-open checklist items; timestamp historical counts. |
| #41 | Open, enhancement | `Finish device validation for paginated session rollout` | Automated rollout is complete; only human device observations remain. Link #38 and #39. |
| #50 | Conditional close | Close only after runner proof; otherwise update current status and keep open | Require allowed-job, rejected-PR, cleanup, and restart proof with run/check identifiers. |

Use only existing labels that match the repository taxonomy: `bug`, `enhancement`, and `documentation`. Do not create new labels during this audit.

- [ ] **Step 3: Rewrite open issue bodies in place**

For each retained issue, use this compact structure when it improves clarity:

```markdown
## Current status

One paragraph describing what exists now and what remains.

## Scope

- Concrete remaining work
- Current constraint or dependency

## Acceptance criteria

- Observable completion condition
```

Preserve useful technical details and linked evidence. Remove generated footers, private-machine paths, stale deployment claims, em dashes, promotional language, and irrelevant legacy-product positioning. Do not rewrite closed historical discussions.

- [ ] **Step 4: Comment before closing superseded issues**

For #14, post a short evidence comment naming issue #47, PR #49, and its merge commit, then close it as completed or superseded according to the repository’s available close reasons.

For #50, post the exact runner proof from the completed runner task and close only if all four evidence gates passed. If any gate is incomplete, update the body with current status and leave it open.

- [ ] **Step 5: Read every changed issue back**

Fetch each edited issue again and compare title, body, state, labels, and new comments with the intended value. Check the returned public text for:

```text
—
formerly Nexus
/Users/
/home/
private IPv4 addresses
tailnet hostnames
credential-shaped assignments
runner registration tokens
```

Redact findings in logs and fix the source record. Never print secret-shaped values into the task report.

- [ ] **Step 6: Verify issue counts and closure evidence**

If #14 and #50 both close, expect 13 open issues from the original 15. If runner proof is incomplete, expect 14 and document why #50 remains open. Confirm every closed issue has a public evidence comment and every edited open issue states current remaining work.

---

## Task 8: Final verification and handoff

**Files:**

- Modify privately: task report under the existing git-ignored SDD workspace

- [ ] **Step 1: Verify merged repository state**

```bash
git fetch origin
git merge-base --is-ancestor <copy-merge-sha> origin/main
gh pr checks <copy-pr-number>
gh repo view arniesaha/drover --json description,visibility
gh issue list --repo arniesaha/drover --state open --limit 100 --json number,title,labels
```

- [ ] **Step 2: Re-run objective checks on merged `main`**

From a clean checkout of merged `origin/main`:

```bash
.venv/bin/python scripts/check_public_release.py
uv run pytest -q tests/test_check_public_release.py
rg -n '—|[Ff]ormerly.{0,80}[Nn]exus' README.md docs apps/drover/README.md skills
git status --short
```

Expected: zero public-release findings, focused tests pass, no scoped copy matches, and the checkout is clean.

- [ ] **Step 3: Write the private completion report**

Record commit and PR identifiers, hosted check run/job IDs, exact metadata read-back, the 15-row final issue classification, closure evidence, final issue count, verification commands, and redacted outcomes. Do not include private backup contents, sensitive paths, endpoints, tokens, or credentials.

- [ ] **Step 4: Request final independent review**

Use `superpowers:requesting-code-review` for the final merged and external-state report. Resolve every critical or major finding before claiming completion.

- [ ] **Step 5: Complete only after evidence is current**

Use `superpowers:verification-before-completion`. Report any intentionally open issue and its remaining gate plainly. Delete no private backup until exact read-back and final review are complete.
