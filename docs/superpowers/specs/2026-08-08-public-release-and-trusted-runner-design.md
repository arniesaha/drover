# Public Release and Trusted Runner Design

**Date:** 2026-08-08  
**Scope:** PR #49, issues #47 and #50

## Goal

Complete Drover's public-readiness hardening, publish the repository, protect
`main`, and add a repository-scoped Mac Mini runner without allowing public
pull-request code to execute under the operator's macOS account.

The public repository will continue to use standard GitHub-hosted runners for
pull-request validation. The self-hosted runner is a trusted, post-merge
verification path, not a replacement for the public contribution boundary.

## Current State

- PR #49 contains the runtime portability and public-facing documentation work
  for issue #47.
- The repository is private and owned by a personal GitHub Free account.
- Hosted Actions have exhausted the private-repository 2,000-minute allowance.
- The current plan does not expose branch protection for this private
  repository, so `main` has no enforceable required checks yet.
- The Mac Mini already has the Python, Xcode, XcodeGen, simulator, and storage
  needed by Drover's CI, but its normal user account also has local credentials
  and private-network access.

Once the repository is public, standard GitHub-hosted Actions are free and
branch protection is available on GitHub Free. That makes hosted PR checks both
the safer and simpler public-contribution boundary.

## Approaches Considered

### Run every job on the Mac Mini

This would replace hosted minutes directly, but a public fork can propose or
modify a workflow that targets a repository-scoped self-hosted runner. Labels
and workflow-level `if` expressions are routing conveniences, not a sufficient
host security boundary. This approach is rejected.

### Do not install a self-hosted runner

Making the repository public removes the hosted-minute constraint, so Drover
could remain fully GitHub-hosted. This is safe and operationally simple, but it
does not provide the requested verification against the actual long-lived Mac
environment and does not satisfy issue #50's runner lifecycle goals.

### Hosted PR checks plus a guarded trusted runner

This is the selected design. Pull requests run only on disposable GitHub-hosted
runners. A persistent repository-scoped runner verifies protected `main` and
explicit owner dispatches. A host-owned pre-job hook fails closed before any
workflow step if the assigned job is outside the allowlist.

## Workflow Architecture

| Event | Python CI | iOS CI | Trusted Mac verification |
| --- | --- | --- | --- |
| Pull request to `main` | GitHub-hosted Ubuntu | GitHub-hosted macOS | Rejected by host hook |
| Push to protected `main` | GitHub-hosted Ubuntu | GitHub-hosted macOS | Self-hosted Mac Mini |
| Owner manual dispatch from `main` | As currently supported | GitHub-hosted macOS | Self-hosted Mac Mini |
| Fork or other branch | GitHub-hosted when approved | GitHub-hosted when approved | Rejected by host hook |

The existing check identities remain stable:

- `CI / build-and-test`
- `iOS CI / Build and test iOS app`

A separate workflow, `Trusted Mac verification`, will target
`[self-hosted, macOS, ARM64, drover-ci]`. It will run the Python suite and iOS
build/test path on `push` to `main`, with an owner-only `workflow_dispatch`
escape hatch. It is supplementary and will not be a required pull-request
check.

All workflows will declare least-privilege `contents: read` permissions. Public
fork workflows will require approval for all external contributors, but that
approval setting is defense in depth and is not relied upon to protect the Mac.

## Runner Security Boundary

The runner will be registered only to `arniesaha/drover`, under the existing
macOS account, with the custom label `drover-ci`. The short-lived registration
token will be obtained at install time and will not be written to the repository
or documentation.

Before registration, host-owned scripts will be installed outside both the
repository and runner application directories. The runner's `.env` will point
`ACTIONS_RUNNER_HOOK_JOB_STARTED` and
`ACTIONS_RUNNER_HOOK_JOB_COMPLETED` at those absolute paths.

The pre-job hook will parse GitHub's event payload and default variables and
allow a job only when all relevant assertions pass:

1. `GITHUB_REPOSITORY` is exactly `arniesaha/drover`.
2. `GITHUB_WORKFLOW_REF` identifies the committed trusted-runner workflow from
   `refs/heads/main`.
3. The event is either a push whose ref is exactly `refs/heads/main`, or a
   `workflow_dispatch` from `arniesaha` whose workflow ref is on `main`.
4. Required metadata exists and parses successfully.

Missing, malformed, or unexpected metadata exits nonzero before any checkout or
repository-controlled command runs. The hook will not print event payloads or
credentials into public Actions logs.

This protects against an external pull request adding a new workflow that names
the self-hosted labels: GitHub may assign the job, but the host hook rejects it
before its first step. Branch protection and human review remain the trust gate
for code that eventually reaches `main`.

## Isolation and Cleanup

The existing account choice means the runner is not an operating-system sandbox.
Trusted jobs can access resources available to that account. The design limits
which code becomes trusted rather than claiming stronger process isolation.

- No repository or deployment secrets will be added for test jobs.
- The runner service receives only the environment required for Python, Xcode,
  XcodeGen, and GitHub runner operation.
- Checkouts, temporary result bundles, and DerivedData stay beneath the known
  runner work and temp roots.
- The post-job hook removes only validated, runner-owned Drover work and temp
  paths. It must refuse broad, empty, home-directory, or filesystem-root targets.
- Cleanup does not touch the Drover development checkout, signing material,
  unrelated simulators, or live Drover services.
- Logs remain in the runner diagnostic directory and must not contain the
  registration token or local secrets.

## Lifecycle and Recovery

The GitHub runner application will live in a dedicated directory under the
existing account and use GitHub's macOS launchd service integration. The runbook
will document:

- registration, labels, and repository scope;
- `svc.sh` install, start, stop, status, and uninstall operations;
- the GitHub runner page and local diagnostic logs;
- automatic runner updates and how to perform a controlled manual update;
- restart recovery after logout or host reboot;
- hook verification with one allowed job and one deliberately rejected job;
- safe deregistration and local removal.

The service will be considered recovered only when GitHub reports the runner
online and a trusted smoke workflow completes after a service restart.

## Public Release Gate

Publishing is an external, irreversible disclosure of the current tree and Git
history. Before changing visibility, the implementation must:

1. Run the repository public-release scanner and its regression tests on the
   PR #49 merge candidate.
2. Search tracked files and reachable history for credentials, private keys,
   operator-specific paths, hosts, tokens, and unintended private artifacts.
3. Review the complete public diff, README, license, security guidance,
   screenshots, issue content, and workflow permissions.
4. Confirm that any remaining historical compatibility identifiers are data or
   API contracts rather than live operator configuration.
5. Stop before publication if any credential or material private artifact is
   found; rotation or history remediation requires a separate explicit decision.

## Publication and Protection Sequence

The ordering avoids exposing the self-hosted runner during the visibility
transition and works around the current private-repository Actions quota:

1. Update PR #49 with this design, runner workflow, runbook, hooks, and tests.
2. Perform local verification and review the complete merge candidate.
3. Merge PR #49 while private; hosted checks may remain quota-blocked, so the
   merge requires explicit local evidence and owner override.
4. Change repository visibility to public.
5. Require workflow approval for every external contributor and restrict
   Actions permissions to the required GitHub-owned actions.
6. Re-run the merge commit's hosted Python and iOS workflows under public-repo
   billing and verify both check identities.
7. Protect `main` with strict required checks, required pull requests, resolved
   conversations, administrator enforcement, and force-push/deletion disabled.
8. Use zero required approving reviews initially because this personal
   repository has one maintainer and authors cannot approve their own pull
   requests. Checks and the pull-request path remain mandatory.
9. Install the host hooks, register the repo-scoped runner, and start its launchd
   service.
10. Run allowed, rejected, cleanup, and restart-recovery verification, then close
    issue #50.

The runner is deliberately registered after publication controls are active.
There is therefore no interval where an unguarded self-hosted runner is attached
to the public repository.

## Verification

Completion requires evidence for all of the following:

- the combined PR #49 public-release scanner and full Python suite pass locally;
- DroverKit and iOS build/test validation pass locally in proportion to the
  workflow changes;
- the repository is public and external-contributor workflow approval is set to
  `all_external_contributors`;
- hosted Python and iOS checks pass on the public repository;
- `main` branch protection reports the two required check contexts and the
  intended review, conversation, admin, force-push, and deletion settings;
- GitHub reports the Mac Mini runner online with the expected labels;
- a trusted `main` or owner-dispatched workflow passes on the Mac Mini;
- a disallowed event is rejected by the pre-job hook before checkout;
- bounded cleanup is demonstrated without affecting the development checkout;
- the runner returns online and accepts a trusted job after service restart.

## Out of Scope

- Executing public pull-request code on the Mac Mini.
- Treating the existing macOS account as a sandbox.
- Adding signing, deployment, production, or LAN credentials to GitHub Actions.
- Rewriting published Git history unless the release audit finds material that
  cannot safely become public.
- Requiring an independent approving review before a second maintainer exists.
