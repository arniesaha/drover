# Public Release and Trusted Runner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete PR #49, publish Drover safely, protect `main`, and operate a repository-scoped Mac Mini runner that accepts only trusted post-merge or owner-dispatched work.

**Architecture:** GitHub-hosted Ubuntu and macOS runners remain the public pull-request boundary and become free after publication. A separate trusted workflow targets the persistent Mac Mini, while a host-owned pre-job hook validates repository, workflow ref, event, ref, actor, and event payload before any repository-controlled step; a bounded post-job hook cleans only runner-owned paths.

**Tech Stack:** GitHub Actions, GitHub REST API and `gh`, macOS ARM64, launchd, Bash, Python 3.11+, pytest, PyYAML, Xcode/XcodeGen, GitHub Actions runner 2.336.0 or the current API-advertised successor.

## Global Constraints

- Public pull-request code must never execute on the Mac Mini.
- The self-hosted runner must be registered only to `arniesaha/drover` and carry labels `self-hosted`, `macOS`, `ARM64`, and `drover-ci`.
- The existing macOS account is trusted but is not an operating-system sandbox.
- The host guard must fail closed before checkout when required metadata is absent, malformed, or outside the allowlist.
- Only `push` to `refs/heads/main` and `workflow_dispatch` by `arniesaha` from `main` may run the trusted workflow.
- Hosted required-check identities remain `CI / build-and-test` and `iOS CI / Build and test iOS app`.
- No signing, deployment, production, repository, Drover API, or LAN credentials may be added to GitHub Actions.
- Host cleanup may remove only validated descendants of the dedicated runner work root; it must reject empty, root, home, or unrelated targets.
- Publication stops if the release audit finds a credential or material private artifact; rotation or history rewriting requires a separate explicit decision.
- `main` protection requires pull requests, strict required checks, resolved conversations, administrator enforcement, and disabled force-push/deletion, with zero approving reviews until a second maintainer exists.

---

## File Map

- `scripts/github_runner/pre_job_guard.py`: pure validation logic and CLI for the fail-closed pre-job policy.
- `scripts/github_runner/pre_job.sh`: GitHub-supported Bash hook entrypoint that invokes the Python guard from its installed host directory.
- `scripts/github_runner/post_job_cleanup.py`: validated cleanup logic for workspace and temp descendants.
- `scripts/github_runner/post_job.sh`: GitHub-supported Bash hook entrypoint for cleanup.
- `tests/test_github_runner_hooks.py`: synthetic event and filesystem tests for both hooks.
- `.github/workflows/trusted-mac.yml`: supplementary Python and iOS verification on the guarded Mac Mini.
- `.github/workflows/ci.yml`: retain hosted PR validation, add manual dispatch and explicit read-only permissions.
- `.github/workflows/ios.yml`: retain hosted PR validation and add explicit read-only permissions.
- `tests/test_github_workflows.py`: structural workflow tests that prevent a PR trigger or non-self-hosted labels from reaching the trusted workflow.
- `docs/github-actions-runner.md`: public operator runbook for security boundary, installation, lifecycle, updates, cleanup, removal, and recovery.
- `docs/security.md`: link the runner boundary to Drover's broader trust model.
- `README.md`: link the runner runbook from contributor/operator documentation.
- `docs/superpowers/`: temporary internal planning material removed from the public-facing tree before publication, as already required by `scripts/check_public_release.py`.

### Task 1: Fail-Closed Pre-Job Guard

**Files:**
- Create: `scripts/github_runner/pre_job_guard.py`
- Create: `scripts/github_runner/pre_job.sh`
- Create: `tests/test_github_runner_hooks.py`

**Interfaces:**
- Consumes: GitHub default variables `GITHUB_REPOSITORY`, `GITHUB_WORKFLOW_REF`, `GITHUB_EVENT_NAME`, `GITHUB_REF`, `GITHUB_ACTOR`, and `GITHUB_EVENT_PATH`.
- Produces: `validate_job(environ: Mapping[str, str], payload: Mapping[str, object]) -> None`, raising `GuardError` on rejection; CLI exit `0` for accepted jobs and `1` for rejected jobs.

- [ ] **Step 1: Write the failing allowed-event and rejection tests**

Add helpers and cases to `tests/test_github_runner_hooks.py` that import the script by path, build a base environment, and assert:

```python
def push_payload() -> dict[str, object]:
    return {
        "ref": "refs/heads/main",
        "repository": {"full_name": "arniesaha/drover"},
        "sender": {"login": "arniesaha"},
    }


def base_env(tmp_path: Path, payload: dict[str, object]) -> dict[str, str]:
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(payload))
    return {
        "GITHUB_REPOSITORY": "arniesaha/drover",
        "GITHUB_WORKFLOW_REF": (
            "arniesaha/drover/.github/workflows/trusted-mac.yml@refs/heads/main"
        ),
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_ACTOR": "arniesaha",
        "GITHUB_EVENT_PATH": str(event_path),
    }


def test_pre_job_accepts_push_to_main(tmp_path: Path) -> None:
    env = base_env(tmp_path, push_payload())
    PRE_JOB.validate_job(env, push_payload())


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("GITHUB_REPOSITORY", "attacker/fork"),
        ("GITHUB_EVENT_NAME", "pull_request"),
        ("GITHUB_REF", "refs/pull/7/merge"),
        (
            "GITHUB_WORKFLOW_REF",
            "arniesaha/drover/.github/workflows/evil.yml@refs/heads/main",
        ),
    ],
)
def test_pre_job_rejects_untrusted_metadata(
    tmp_path: Path, key: str, value: str
) -> None:
    env = base_env(tmp_path, push_payload())
    env[key] = value
    with pytest.raises(PRE_JOB.GuardError):
        PRE_JOB.validate_job(env, push_payload())
```

Also add explicit tests for an owner `workflow_dispatch` with payload ref
`main`, a dispatch by another actor, mismatched payload repository/ref/sender,
missing variables, a missing event file, and malformed JSON.

- [ ] **Step 2: Run the new tests and confirm the guard does not exist**

Run: `uv run pytest tests/test_github_runner_hooks.py -q`

Expected: FAIL during import because `scripts/github_runner/pre_job_guard.py` is absent.

- [ ] **Step 3: Implement the minimal validation module**

Create constants and validation with this exact contract:

```python
EXPECTED_REPOSITORY = "arniesaha/drover"
EXPECTED_WORKFLOW_REF = (
    "arniesaha/drover/.github/workflows/trusted-mac.yml@refs/heads/main"
)
EXPECTED_OWNER = "arniesaha"


class GuardError(RuntimeError):
    pass


def validate_job(
    environ: Mapping[str, str], payload: Mapping[str, object]
) -> None:
    required = (
        "GITHUB_REPOSITORY",
        "GITHUB_WORKFLOW_REF",
        "GITHUB_EVENT_NAME",
        "GITHUB_REF",
        "GITHUB_ACTOR",
        "GITHUB_EVENT_PATH",
    )
    missing = [key for key in required if not environ.get(key)]
    if missing:
        raise GuardError("required GitHub job metadata is missing")
    if environ["GITHUB_REPOSITORY"] != EXPECTED_REPOSITORY:
        raise GuardError("repository is not allowed")
    if environ["GITHUB_WORKFLOW_REF"] != EXPECTED_WORKFLOW_REF:
        raise GuardError("workflow ref is not allowed")

    repository = payload.get("repository")
    sender = payload.get("sender")
    if not isinstance(repository, Mapping) or repository.get("full_name") != EXPECTED_REPOSITORY:
        raise GuardError("event repository does not match")
    if not isinstance(sender, Mapping):
        raise GuardError("event sender is missing")

    event = environ["GITHUB_EVENT_NAME"]
    if event == "push":
        if environ["GITHUB_REF"] != "refs/heads/main" or payload.get("ref") != "refs/heads/main":
            raise GuardError("push ref is not allowed")
        return
    if event == "workflow_dispatch":
        if (
            environ["GITHUB_REF"] != "refs/heads/main"
            or payload.get("ref") != "main"
            or environ["GITHUB_ACTOR"] != EXPECTED_OWNER
            or sender.get("login") != EXPECTED_OWNER
        ):
            raise GuardError("dispatch metadata is not allowed")
        return
    raise GuardError("event is not allowed")
```

`main()` must load only `GITHUB_EVENT_PATH`, catch JSON/I/O/type errors, emit a
short reason without payload contents, and return `1`; success returns `0`.
Create executable `pre_job.sh` with:

```bash
#!/bin/bash
set -euo pipefail
hook_dir="$(cd "$(dirname "$0")" && pwd)"
exec /usr/bin/env python3 "$hook_dir/pre_job_guard.py"
```

- [ ] **Step 4: Run focused tests and formatting**

Run: `uv run pytest tests/test_github_runner_hooks.py -q`

Expected: all pre-job cases PASS.

Run: `uv run black --check scripts/github_runner/pre_job_guard.py tests/test_github_runner_hooks.py`

Expected: PASS.

- [ ] **Step 5: Commit the pre-job guard**

```bash
git add scripts/github_runner/pre_job_guard.py scripts/github_runner/pre_job.sh tests/test_github_runner_hooks.py
git commit -m "feat(ci): reject untrusted self-hosted jobs"
```

### Task 2: Bounded Post-Job Cleanup

**Files:**
- Create: `scripts/github_runner/post_job_cleanup.py`
- Create: `scripts/github_runner/post_job.sh`
- Modify: `tests/test_github_runner_hooks.py`

**Interfaces:**
- Consumes: `DROVER_RUNNER_WORK_ROOT`, `GITHUB_WORKSPACE`, and `RUNNER_TEMP`.
- Produces: `cleanup_job(environ: Mapping[str, str]) -> tuple[Path, ...]`, returning removed paths and raising `CleanupError` before deleting an unsafe target.

- [ ] **Step 1: Add failing cleanup boundary tests**

Add tests that create a temporary `_work/drover/drover` checkout, `_work/_temp`
contents, and an unrelated sibling. Assert safe descendants are removed and the
sibling remains. Add parameterized rejection cases for an empty work root,
filesystem root, home directory, a workspace equal to the root, a workspace
outside the root, a workspace whose final component is not `drover`, and a temp
directory whose final component is not `_temp`.

Core success assertion:

```python
removed = POST_JOB.cleanup_job(
    {
        "DROVER_RUNNER_WORK_ROOT": str(work_root),
        "GITHUB_WORKSPACE": str(workspace),
        "RUNNER_TEMP": str(temp_dir),
    }
)
assert removed == (workspace.resolve(), temp_dir.resolve())
assert not workspace.exists()
assert not temp_dir.exists()
assert unrelated.read_text() == "keep"
```

- [ ] **Step 2: Run the focused cleanup tests and confirm failure**

Run: `uv run pytest tests/test_github_runner_hooks.py -q`

Expected: FAIL because `post_job_cleanup.py` is absent.

- [ ] **Step 3: Implement path validation and deletion**

Implement `CleanupError`, `_validated_root`, `_validated_child`, and
`cleanup_job`. Resolve paths without requiring them to exist, reject root and
the current user's resolved home, require both targets to be strict descendants
of the configured root, require workspace name `drover` and temp name `_temp`,
validate all targets before any deletion, then use `shutil.rmtree` only for
existing targets.

Create executable `post_job.sh`:

```bash
#!/bin/bash
set -euo pipefail
hook_dir="$(cd "$(dirname "$0")" && pwd)"
exec /usr/bin/env python3 "$hook_dir/post_job_cleanup.py"
```

- [ ] **Step 4: Run hook tests, formatting, and shell syntax checks**

Run: `uv run pytest tests/test_github_runner_hooks.py -q`

Expected: PASS.

Run: `uv run black --check scripts/github_runner/*.py tests/test_github_runner_hooks.py`

Expected: PASS.

Run: `bash -n scripts/github_runner/pre_job.sh scripts/github_runner/post_job.sh`

Expected: exit `0`.

- [ ] **Step 5: Commit bounded cleanup**

```bash
git add scripts/github_runner/post_job_cleanup.py scripts/github_runner/post_job.sh tests/test_github_runner_hooks.py
git commit -m "feat(ci): clean self-hosted runner work safely"
```

### Task 3: Hosted PR Checks and Trusted Mac Workflow

**Files:**
- Create: `.github/workflows/trusted-mac.yml`
- Create: `tests/test_github_workflows.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `.github/workflows/ios.yml`

**Interfaces:**
- Consumes: host labels and hook policy from Tasks 1-2.
- Produces: hosted required checks with unchanged names and a self-hosted-only `Trusted Mac verification` workflow at the exact path allowed by the guard.

- [ ] **Step 1: Write failing workflow structure tests**

Use `yaml.load(path.read_text(), Loader=yaml.BaseLoader)` so YAML 1.1 does not
coerce the `on` key. Assert:

```python
def test_public_pr_workflows_stay_github_hosted() -> None:
    python_ci = load_workflow("ci.yml")
    ios_ci = load_workflow("ios.yml")
    assert python_ci["permissions"] == {"contents": "read"}
    assert ios_ci["permissions"] == {"contents": "read"}
    assert python_ci["jobs"]["build-and-test"]["runs-on"] == "ubuntu-latest"
    assert ios_ci["jobs"]["build-and-test"]["runs-on"] == "macos-15"
    assert "pull_request" in python_ci["on"]
    assert "pull_request" in ios_ci["on"]


def test_trusted_workflow_has_no_pull_request_trigger() -> None:
    workflow = load_workflow("trusted-mac.yml")
    assert workflow["permissions"] == {"contents": "read"}
    assert set(workflow["on"]) == {"push", "workflow_dispatch"}
    assert workflow["on"]["push"]["branches"] == ["main"]
    for job in workflow["jobs"].values():
        assert job["runs-on"] == ["self-hosted", "macOS", "ARM64", "drover-ci"]
```

Also assert CI has `workflow_dispatch`, trusted job display names are unique,
and no trusted job defines an `if` condition as a substitute for the host hook.

- [ ] **Step 2: Run workflow tests and confirm failure**

Run: `uv run pytest tests/test_github_workflows.py -q`

Expected: FAIL because permissions and `trusted-mac.yml` are absent.

- [ ] **Step 3: Add least privilege and the trusted workflow**

Add to both existing workflows immediately after `on`:

```yaml
permissions:
  contents: read
```

Add `workflow_dispatch:` to `.github/workflows/ci.yml` without changing its
hosted runner or job ID. Create `trusted-mac.yml` with `push` to `main` and
`workflow_dispatch`, read-only permissions, concurrency keyed by ref, and two
jobs on `[self-hosted, macOS, ARM64, drover-ci]`:

- `python`: checkout with full history, setup Python 3.11 with pip cache,
  install `.[dev]`, configure git identity locally for tests, Black-check changed
  Python files, and run `pytest tests/`.
- `ios`: checkout, show Xcode version, require or install XcodeGen, generate the
  project in `apps/drover`, select an available iPhone simulator, resolve Swift
  packages, run unit tests, build the UI test bundle, and upload result bundles
  on failure.

Use job names `Python on trusted Mac` and `iOS on trusted Mac`. Keep the existing
hosted workflow job IDs and names unchanged so branch-protection contexts stay
stable.

- [ ] **Step 4: Run structural tests and parse all workflows**

Run: `uv run pytest tests/test_github_workflows.py -q`

Expected: PASS.

Run:

```bash
uv run python - <<'PY'
from pathlib import Path
import yaml
for path in Path('.github/workflows').glob('*.yml'):
    yaml.safe_load(path.read_text())
    print(path)
PY
```

Expected: every workflow path prints with no exception.

- [ ] **Step 5: Commit workflows and their contract tests**

```bash
git add .github/workflows/ci.yml .github/workflows/ios.yml .github/workflows/trusted-mac.yml tests/test_github_workflows.py
git commit -m "feat(ci): add guarded trusted Mac verification"
```

### Task 4: Runner Runbook and Public-Facing Security Guidance

**Files:**
- Create: `docs/github-actions-runner.md`
- Modify: `docs/security.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: exact labels, hook paths, variables, workflow name, and lifecycle behavior from Tasks 1-3.
- Produces: a public runbook sufficient to install, monitor, update, restart, test, and remove the runner without exposing registration tokens.

- [ ] **Step 1: Draft the runbook with exact host layout and commands**

Document this host layout using environment variables rather than personal
paths:

```bash
export DROVER_RUNNER_DIR="$HOME/actions-runner/drover"
export DROVER_RUNNER_HOOK_DIR="$HOME/.config/drover/actions-runner/hooks"
export DROVER_RUNNER_WORK_ROOT="$DROVER_RUNNER_DIR/_work"
```

Include exact API queries for the current macOS ARM64 download metadata and the
short-lived repo registration token, SHA-256 verification, `config.sh` flags
`--url https://github.com/arniesaha/drover --name drover-mac-mini --labels drover-ci --work _work --unattended --replace`, `.env` hook entries, and non-root
`svc.sh install/start/status/stop/uninstall` commands.

State prominently that public PRs remain hosted, labels are not a security
boundary, the hook copy lives outside the checkout, and approving an external
workflow does not authorize it for the Mac. Include monitoring through the
repository runner page and `_diag`, automatic update behavior, bounded cleanup,
reboot recovery, token handling, deregistration, and the rejection-probe test.

- [ ] **Step 2: Link the runbook from security and README**

Add a `GitHub Actions Runner` subsection to `docs/security.md` that summarizes
the existing-account risk and links to `github-actions-runner.md`. Add the same
runbook to the README documentation list without adding local hostnames, paths,
or secrets.

- [ ] **Step 3: Run documentation and public-release checks**

Run: `uv run pytest tests/test_check_public_release.py -q`

Expected: PASS.

Run: `uv run python scripts/check_public_release.py`

Expected at this point: findings only for the intentionally temporary
`docs/superpowers/` planning tree; no finding may reference the new runbook,
security guidance, hooks, workflows, or README.

- [ ] **Step 4: Commit the public runbook**

```bash
git add README.md docs/security.md docs/github-actions-runner.md
git commit -m "docs: add trusted runner operations runbook"
```

### Task 5: Final Public-Tree Audit and PR #49 Validation

**Files:**
- Delete: `docs/superpowers/`
- Verify: all tracked files and reachable Git history

**Interfaces:**
- Consumes: the complete PR #49 merge candidate.
- Produces: a scanner-clean public tree and recorded local verification evidence; does not rewrite history.

- [ ] **Step 1: Remove internal planning files from the visible tree**

Run: `git rm -r docs/superpowers`

Expected: only the internal specs and plans are staged for deletion. They remain
recoverable from prior commits; no Git history is rewritten.

- [ ] **Step 2: Run tree and history disclosure audits**

Run: `uv run python scripts/check_public_release.py`

Expected: `Public release audit: 0 finding(s)`.

Install Gitleaks if absent, then scan reachable history with redaction:

```bash
brew list gitleaks >/dev/null 2>&1 || brew install gitleaks
gitleaks git --redact --no-banner --verbose --log-opts="--all" .
```

Expected: exit `0` and no leaks. If Gitleaks reports anything, inspect only its
redacted file, rule, and commit metadata. A true credential or material private
artifact stops publication; do not put its value in commentary, logs, commits,
or PR text.

Review operator-specific and sensitive patterns without printing suspected
credential values:

```bash
git grep -n -I -E '/Users/|/home/|192\.168\.|10\.[0-9]+\.|\.ts\.net|BEGIN .*PRIVATE KEY' -- ':!tests/**' ':!apps/drover/DroverKit/Tests/**'
```

Expected: no unexplained public-facing operator values. Classify compatibility
fixtures separately from live configuration.

Confirm `LICENSE`, README links, security guidance, architecture assets, and iOS
screenshots are present and suitable for public display. List all GitHub issues,
pull requests, releases, and repository metadata through `gh`; inspect their
titles, bodies, comments, and attachments for private paths, hosts, tokens, or
unintended personal content without echoing suspected credential values.

- [ ] **Step 3: Run complete Python verification**

Run: `uv sync --extra dev`

Run: `uv run black --check src tests scripts`

Run: `uv run pytest -q`

Expected: all commands exit `0`.

- [ ] **Step 4: Run DroverKit and generated-project iOS verification**

Run: `swift test --package-path apps/drover/DroverKit`

Expected: all Swift Testing and XCTest cases pass.

Run:

```bash
cd apps/drover
xcodegen generate
simulator_id="$(xcrun simctl list devices available | awk -F '[()]' '/iPhone/ { print $2; exit }')"
xcodebuild -project Drover.xcodeproj -scheme Drover -destination "id=$simulator_id" test
xcodebuild -project Drover.xcodeproj -scheme DroverUITests -destination "id=$simulator_id" build-for-testing
```

Expected: both `xcodebuild` commands end with `** TEST SUCCEEDED **` or
`** BUILD SUCCEEDED **` respectively.

- [ ] **Step 5: Commit the public-tree cleanup**

```bash
git add -u docs/superpowers
git commit -m "chore: remove internal release planning docs"
```

- [ ] **Step 6: Review and push the refreshed PR branch**

Run: `git diff --check origin/main...HEAD`

Run: `git log --oneline origin/main..HEAD`

Run: `git status --short`

Expected: no whitespace errors, only intended PR commits, and a clean worktree.

Push: `git push origin fix/47-public-v01-hardening`

Update PR #49's body with the combined #47/#50 scope, security boundary, current
validation counts, and the note that hosted checks are quota-blocked only while
private. Do not put memory citations, runner tokens, host paths, or audit
findings in the PR body.

### Task 6: Merge, Publish, Enable Hosted Checks, and Protect `main`

**Files:**
- External GitHub state only

**Interfaces:**
- Consumes: reviewed, locally verified PR #49 and stable hosted check names.
- Produces: public repository with safe Actions policy and protected `main`.

- [ ] **Step 1: Review PR #49 and merge while the repository is private**

Run: `gh pr diff 49 --repo arniesaha/drover` and inspect the complete diff.

Run: `gh pr checks 49 --repo arniesaha/drover`

Expected: any failure occurs before job steps because of private hosted-minute
quota, not test execution. Merge only with the fresh local evidence from Task 5:

```bash
gh pr merge 49 --repo arniesaha/drover --merge --admin
```

Capture the remote merge commit and confirm issue #47 closes. Keep issue #50
open until the runner is live.

- [ ] **Step 2: Confirm no runner is attached, then make the repository public**

Run: `gh api repos/arniesaha/drover/actions/runners --jq '.total_count'`

Expected: `0`.

Run:

```bash
gh api -X PATCH repos/arniesaha/drover -f visibility=public
gh repo view arniesaha/drover --json visibility,isPrivate,url
```

Expected: visibility `PUBLIC`, `isPrivate: false`.

- [ ] **Step 3: Apply public Actions policy before runner registration**

Run:

```bash
gh api -X PUT repos/arniesaha/drover/actions/permissions \
  -F enabled=true -f allowed_actions=selected
gh api -X PUT repos/arniesaha/drover/actions/permissions/selected-actions \
  -F github_owned_allowed=true -F verified_allowed=false \
  -f 'patterns_allowed[]=actions/*'
gh api -X PUT repos/arniesaha/drover/actions/permissions/fork-pr-contributor-approval \
  -f approval_policy=all_external_contributors
```

Read all three endpoints back. Expected: Actions enabled, only GitHub-owned
`actions/*` permitted, and approval required for all external contributors.

- [ ] **Step 4: Run and verify both public hosted checks**

Dispatch both workflows from the merge commit:

```bash
gh workflow run ci.yml --repo arniesaha/drover --ref main
gh workflow run ios.yml --repo arniesaha/drover --ref main
gh run list --repo arniesaha/drover --branch main --limit 10
```

Watch the matching runs with `gh run watch RUN_ID --exit-status`. Expected:
`build-and-test` and `Build and test iOS app` both pass on standard hosted
runners without consuming the private allowance.

- [ ] **Step 5: Protect `main` with the passing check contexts**

Apply classic branch protection through the REST API using this payload:

```json
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["build-and-test", "Build and test iOS app"]
  },
  "enforce_admins": true,
  "required_pull_request_reviews": {
    "dismiss_stale_reviews": false,
    "require_code_owner_reviews": false,
    "required_approving_review_count": 0,
    "require_last_push_approval": false
  },
  "restrictions": null,
  "required_conversation_resolution": true,
  "required_linear_history": false,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "block_creations": false,
  "lock_branch": false,
  "allow_fork_syncing": true
}
```

Send the exact payload without creating a tracked file:

```bash
gh api -X PUT repos/arniesaha/drover/branches/main/protection --input - <<'JSON'
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["build-and-test", "Build and test iOS app"]
  },
  "enforce_admins": true,
  "required_pull_request_reviews": {
    "dismiss_stale_reviews": false,
    "require_code_owner_reviews": false,
    "required_approving_review_count": 0,
    "require_last_push_approval": false
  },
  "restrictions": null,
  "required_conversation_resolution": true,
  "required_linear_history": false,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "block_creations": false,
  "lock_branch": false,
  "allow_fork_syncing": true
}
JSON
```

Read the protection endpoint back and verify strict contexts, zero approvals,
conversation resolution, admin enforcement, and disabled force-push/deletion.

- [ ] **Step 6: Synchronize local `main` without discarding local commits**

In the primary checkout, fetch origin, inspect `git status`, and merge or rebase
according to its existing local-only commits. Do not reset, force-push, or drop
the two known local commits. Confirm the PR #49 remote merge commit is an
ancestor of local `main` and leave unrelated worktrees untouched.

### Task 7: Install, Register, and Prove the Trusted Mac Runner

**Files:**
- Install copies under: `$HOME/.config/drover/actions-runner/hooks/`
- Install runner under: `$HOME/actions-runner/drover/`
- External GitHub runner and Actions state

**Interfaces:**
- Consumes: merged hook sources, public Actions policy, protected `main`, and `.github/workflows/trusted-mac.yml`.
- Produces: online launchd-managed `drover-mac-mini` runner plus allowed, rejected, cleanup, and restart evidence.

- [ ] **Step 1: Install immutable host hook copies**

Create the dedicated directories and copy the four merged hook files from local
`main` with mode `0755` for `.sh` and `0644` for `.py`. Do not link into the
repository checkout. Set:

```bash
export DROVER_RUNNER_DIR="$HOME/actions-runner/drover"
export DROVER_RUNNER_HOOK_DIR="$HOME/.config/drover/actions-runner/hooks"
export DROVER_RUNNER_WORK_ROOT="$DROVER_RUNNER_DIR/_work"
```

Run the installed pre-job guard against synthetic allowed-push and rejected-PR
payloads. Expected: allowed exits `0`; rejected exits `1` before registration.

- [ ] **Step 2: Download and verify the current macOS ARM64 runner**

Query `repos/arniesaha/drover/actions/runners/downloads` and select
`os == "osx"` and `architecture == "arm64"`. At planning time this is runner
`2.336.0`, filename `actions-runner-osx-arm64-2.336.0.tar.gz`, SHA-256
`8e8839c49b7060b6b2154f4931f815df330c27f167d53ef2239ee3dfce28b079`;
use the API's newer metadata if it changes.

Download to a `mktemp -d` directory, verify with `shasum -a 256 -c`, create the
exact runner directory, and extract the archive there. Do not overwrite a
non-runner directory; if the target exists, inspect it and use the documented
uninstall/removal path first.

- [ ] **Step 3: Register at repository scope and configure hooks**

Obtain a short-lived token without printing it:

```bash
registration_token="$(gh api -X POST repos/arniesaha/drover/actions/runners/registration-token --jq .token)"
```

From `$DROVER_RUNNER_DIR`, run:

```bash
./config.sh --url https://github.com/arniesaha/drover \
  --token "$registration_token" \
  --name drover-mac-mini \
  --labels drover-ci \
  --work _work \
  --unattended \
  --replace
unset registration_token
```

Create runner `.env` containing absolute values for:

```text
ACTIONS_RUNNER_HOOK_JOB_STARTED=<hook-dir>/pre_job.sh
ACTIONS_RUNNER_HOOK_JOB_COMPLETED=<hook-dir>/post_job.sh
DROVER_RUNNER_WORK_ROOT=<runner-dir>/_work
```

The `.env` file must not contain the registration token, repository secrets, or
Drover credentials.

- [ ] **Step 4: Install and start the launchd service**

Run as the existing user, without `sudo`:

```bash
./svc.sh install
./svc.sh start
./svc.sh status
```

Read the runner API. Expected: one online runner named `drover-mac-mini` with
labels `self-hosted`, `macOS`, `ARM64`, and `drover-ci`.

- [ ] **Step 5: Verify a real allowed trusted workflow and cleanup**

Run:

```bash
gh workflow run trusted-mac.yml --repo arniesaha/drover --ref main
```

Watch the run to completion. Expected: `Python on trusted Mac` and `iOS on
trusted Mac` pass. Confirm the runner work checkout and `_temp` contents from
the job were removed, the primary Drover checkout still has the same HEAD and
status, and live `com.drover.server` / `com.drover.harnessd` services remain
healthy.

- [ ] **Step 6: Prove a public-PR job is rejected before checkout**

Create a disposable branch from protected `main` in a temporary worktree. Add
only `.github/workflows/runner-rejection-probe.yml`, triggered by
`pull_request`, with a job targeting `[self-hosted, macOS, ARM64, drover-ci]`
and a first step that would create a uniquely named marker under `$RUNNER_TEMP`.
Push it, open a temporary PR, and observe the job.

Expected: the job fails in `Set up runner` with the generic pre-job rejection,
no checkout or marker step runs, and no sensitive event payload appears in the
public log. Close the PR, delete only the disposable remote/local branch and
temporary worktree, and confirm `main` was never modified.

- [ ] **Step 7: Prove service restart recovery**

Run from the runner directory:

```bash
./svc.sh stop
./svc.sh start
./svc.sh status
```

Wait for the runner API to report online, dispatch `trusted-mac.yml` again, and
require a successful run. Inspect `_diag` for restart errors without publishing
sensitive log contents.

- [ ] **Step 8: Close issue #50 with evidence**

Comment on issue #50 with the public PR/merge, hosted check runs, branch
protection state, runner labels, allowed workflow run, rejected probe run,
cleanup result, and restart-recovery run. Then close #50. Never include host
paths, event payloads, registration tokens, or local credentials.

## Final Verification Checklist

- [ ] `git status --short` is clean in the primary checkout and implementation worktree.
- [ ] PR #49 is merged and its merge commit is present locally and remotely.
- [ ] Repository visibility is public.
- [ ] External-contributor workflow approval is `all_external_contributors`.
- [ ] Actions are limited to the required GitHub-owned `actions/*` actions.
- [ ] Hosted Python and iOS checks pass.
- [ ] `main` protection has both strict required contexts and all approved controls.
- [ ] Runner API reports `drover-mac-mini` online with all four labels.
- [ ] Trusted Python and iOS jobs pass on the Mac Mini.
- [ ] Public-PR rejection occurs before checkout.
- [ ] Post-job cleanup is bounded and leaves development state and live services intact.
- [ ] Runner returns online and passes after a service restart.
- [ ] Issue #50 is closed with non-sensitive evidence.
