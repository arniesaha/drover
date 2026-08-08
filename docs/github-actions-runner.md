# Trusted GitHub Actions Runner

This runbook installs and operates the repository-scoped macOS ARM64 runner
used by [Trusted Mac verification](../.github/workflows/trusted-mac.yml). Run
every command as the dedicated, non-root macOS account that will own the
runner service.

## Security boundary

The runner is a trusted machine, not a general public CI worker.

- Public pull requests stay on the GitHub-hosted workflows in `ci.yml` and
  `ios.yml`. They do not run on this Mac.
- The only trusted workflow is `.github/workflows/trusted-mac.yml`. It runs on
  `push` to `main` or an owner-initiated `workflow_dispatch`, and selects
  `[self-hosted, macOS, ARM64, drover-ci]`.
- **Runner labels are routing hints, not a security boundary.** The host-side
  pre-job hook independently accepts only that repository and the trusted
  workflow at `refs/heads/main`; it rejects every other event before job
  steps run.
- Approving a workflow from an external contributor in GitHub does **not**
  authorize it for this Mac. Do not weaken the hook or add a pull-request
  trigger to the trusted workflow.
- The runner executes as its service account. Anyone able to modify trusted
  `main`, or to control that account, can cause code to run there. Protect the
  repository, the GitHub account, branch rules, and the macOS login as one
  trust domain.

The pre- and post-job hook copies deliberately live outside both the runner
application directory and the checkout. A workflow checkout must never be
able to replace its own admission control.

## Layout and prerequisites

Use the following layout. These variables make the commands portable; do not
replace them with a personal host path in documentation, scripts, or tickets.

```bash
export DROVER_RUNNER_DIR="$HOME/actions-runner/drover"
export DROVER_RUNNER_HOOK_DIR="$HOME/.config/drover/actions-runner/hooks"
export DROVER_RUNNER_WORK_ROOT="$DROVER_RUNNER_DIR/_work"
```

You need a local trusted clone to copy the reviewed hooks from. Set it for the
installation session (and use a checked, reviewed `main`, not a pull-request
checkout):

```bash
export DROVER_SOURCE_DIR="$HOME/src/drover"
```

Install the Xcode, XcodeGen, Python, and other tooling required by the trusted
workflow before registering the runner. Do not use `sudo` for runner setup or
for `svc.sh`; the launchd service must belong to the non-root runner account.

For the GitHub API calls below, provide a fine-grained token with this
repository's **Administration: write** permission. Keep it out of shell
history and terminal recording:

```bash
read -rs "GITHUB_TOKEN?GitHub API token: "
export GITHUB_TOKEN
echo
```

Registration and removal tokens are short-lived (one hour). They are not
stored by this runbook: do not paste them in issues, logs, `.env`, or command
history.

## Download and verify the current runner

Ask GitHub for the runner application supported for this repository, selecting
the `osx`/`arm64` record. The metadata response supplies the filename, download
URL, and SHA-256 checksum.

```bash
RUNNER_METADATA="$(curl --fail --silent --show-error --location \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  -H "X-GitHub-Api-Version: 2026-03-10" \
  "https://api.github.com/repos/arniesaha/drover/actions/runners/downloads")"

RUNNER_URL="$(jq -er '.[] | select(.os == "osx" and .architecture == "arm64") | .download_url' \
  <<<"$RUNNER_METADATA")"
RUNNER_FILENAME="$(jq -er '.[] | select(.os == "osx" and .architecture == "arm64") | .filename' \
  <<<"$RUNNER_METADATA")"
RUNNER_SHA256="$(jq -er '.[] | select(.os == "osx" and .architecture == "arm64") | .sha256_checksum' \
  <<<"$RUNNER_METADATA")"

mkdir -p "$DROVER_RUNNER_DIR"
curl --fail --silent --show-error --location "$RUNNER_URL" \
  --output "$DROVER_RUNNER_DIR/$RUNNER_FILENAME"
printf '%s  %s\n' "$RUNNER_SHA256" "$DROVER_RUNNER_DIR/$RUNNER_FILENAME" \
  | shasum -a 256 -c -
tar -xzf "$DROVER_RUNNER_DIR/$RUNNER_FILENAME" -C "$DROVER_RUNNER_DIR"
rm "$DROVER_RUNNER_DIR/$RUNNER_FILENAME"
```

Stop if `jq` finds no record or `shasum` fails. Do not configure an unchecked
archive.

## Install the host-owned hooks

Copy all four files together. The shell wrappers locate the Python programs in
their own directory, so the filenames and directory layout must remain exact.

```bash
install -d -m 700 "$DROVER_RUNNER_HOOK_DIR"
for hook in pre_job.sh pre_job_guard.py post_job.sh post_job_cleanup.py; do
  install -m 700 \
    "$DROVER_SOURCE_DIR/scripts/github_runner/$hook" \
    "$DROVER_RUNNER_HOOK_DIR/$hook"
done
```

Create the runner's `.env` with absolute, expanded paths. Do not put literal
shell variable references in `.env`; the runner needs the final path values.

```bash
printf '%s\n' \
  "ACTIONS_RUNNER_HOOK_JOB_STARTED=$DROVER_RUNNER_HOOK_DIR/pre_job.sh" \
  "ACTIONS_RUNNER_HOOK_JOB_COMPLETED=$DROVER_RUNNER_HOOK_DIR/post_job.sh" \
  "DROVER_RUNNER_WORK_ROOT=$DROVER_RUNNER_WORK_ROOT" \
  > "$DROVER_RUNNER_DIR/.env"
chmod 600 "$DROVER_RUNNER_DIR/.env"
```

`ACTIONS_RUNNER_HOOK_JOB_STARTED` rejects untrusted job metadata before the
workflow executes. `ACTIONS_RUNNER_HOOK_JOB_COMPLETED` removes only the
validated `drover` checkout and `_temp` directory below
`DROVER_RUNNER_WORK_ROOT`; it fails rather than deleting a broader path. Any
`.env` change requires a runner restart.

## Register and start

Request a fresh repository registration token immediately before configuration:

```bash
RUNNER_REGISTRATION_TOKEN="$(curl --fail --silent --show-error --location \
  --request POST \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  -H "X-GitHub-Api-Version: 2026-03-10" \
  "https://api.github.com/repos/arniesaha/drover/actions/runners/registration-token" \
  | jq -er '.token')"

cd "$DROVER_RUNNER_DIR"
./config.sh --url https://github.com/arniesaha/drover --name drover-mac-mini --labels drover-ci --work _work --unattended --replace --token "$RUNNER_REGISTRATION_TOKEN"
unset RUNNER_REGISTRATION_TOKEN GITHUB_TOKEN

./svc.sh install
./svc.sh start
./svc.sh status
```

`--replace` updates an existing runner registration with that exact name. Open
the repository's [Actions runners page](https://github.com/arniesaha/drover/settings/actions/runners)
and confirm `drover-mac-mini` is `Idle` with the `drover-ci`, `macOS`, and
`ARM64` labels. `Active` means it is running a job; `Offline` means investigate
before dispatching trusted work.

## Test the boundary

First run the runner network check with a GitHub token that has workflow
read/write access. Avoid placing the token directly in the command line:

```bash
read -rs "GITHUB_PAT?GitHub workflow token: "
export GITHUB_PAT
echo
"$DROVER_RUNNER_DIR/config.sh" --check \
  --url https://github.com/arniesaha/drover --pat "$GITHUB_PAT"
unset GITHUB_PAT
```

Then run this local rejection probe. It presents external pull-request metadata
to the installed pre-job hook. The command must print `pre-job guard rejected`
and finish successfully with `rejection confirmed`; if the hook accepts it,
stop the runner and investigate.

```bash
PROBE_DIR="$(mktemp -d)"
printf '%s\n' '{"ref":"refs/pull/1/merge","repository":{"full_name":"attacker/fork"},"sender":{"login":"attacker"}}' \
  > "$PROBE_DIR/event.json"
if env \
  GITHUB_REPOSITORY=attacker/fork \
  GITHUB_WORKFLOW_REF=attacker/fork/.github/workflows/ci.yml@refs/heads/main \
  GITHUB_EVENT_NAME=pull_request \
  GITHUB_REF=refs/pull/1/merge \
  GITHUB_ACTOR=attacker \
  GITHUB_EVENT_PATH="$PROBE_DIR/event.json" \
  "$DROVER_RUNNER_HOOK_DIR/pre_job.sh"; then
  echo "ERROR: untrusted job was accepted" >&2
  exit 1
else
  echo "rejection confirmed"
fi
rm -rf "$PROBE_DIR"
```

Finally, use **Run workflow** for `Trusted Mac verification` on `main`. Check
the job log's `Set up runner` and `Complete runner` entries, then confirm the
runner returns to `Idle` on the repository runners page.

## Routine operations

Use the service commands from the runner directory, always as the same
non-root account:

```bash
cd "$DROVER_RUNNER_DIR"
./svc.sh status
./svc.sh stop
./svc.sh start
./svc.sh uninstall
```

For monitoring, use the repository runners page for `Idle`, `Active`, and
`Offline` state, and inspect `$DROVER_RUNNER_DIR/_diag`. `Runner_` logs record
listener startup and updates; `Worker_` logs record jobs; `SelfUpdate` logs
explain update failures. Do not publish their contents without redacting
tokens, paths, repository data, and user content.

The runner application automatically updates itself when GitHub requires a
newer version, but it does not update macOS, Xcode, Python, Homebrew, or
XcodeGen. Review `_diag` after updates and maintain those host dependencies
separately. The external hook directory and `.env` should survive an
application update; verify them and rerun the rejection probe after any manual
reinstall or service recreation.

The launchd service is registered for the runner account. After a reboot, log
in as that account and run `./svc.sh status`; if it is not running, use
`./svc.sh start`, inspect `_diag`, and confirm GitHub can show it as `Idle`.

## Updating hooks and removing the runner

When updating the trusted hook policy, stop the service, copy all four reviewed
files from a trusted `main` checkout again, start it, and run the rejection
probe. Never point a hook at a job checkout.

```bash
cd "$DROVER_RUNNER_DIR"
./svc.sh stop
for hook in pre_job.sh pre_job_guard.py post_job.sh post_job_cleanup.py; do
  install -m 700 \
    "$DROVER_SOURCE_DIR/scripts/github_runner/$hook" \
    "$DROVER_RUNNER_HOOK_DIR/$hook"
done
./svc.sh start
```

To retire the Mac, wait for the runner to be `Idle`, stop and uninstall its
service, then obtain a fresh **remove** token and deregister it. Do this before
deleting the local directory.

```bash
cd "$DROVER_RUNNER_DIR"
./svc.sh stop
./svc.sh uninstall

read -rs "GITHUB_TOKEN?GitHub API token: "
export GITHUB_TOKEN
echo
RUNNER_REMOVE_TOKEN="$(curl --fail --silent --show-error --location \
  --request POST \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  -H "X-GitHub-Api-Version: 2026-03-10" \
  "https://api.github.com/repos/arniesaha/drover/actions/runners/remove-token" \
  | jq -er '.token')"
./config.sh remove --token "$RUNNER_REMOVE_TOKEN"
unset RUNNER_REMOVE_TOKEN GITHUB_TOKEN
```

Confirm that `drover-mac-mini` no longer appears on the repository runners
page. Only then remove the two exact directories after checking the expanded
values:

```bash
printf '%s\n' "$DROVER_RUNNER_DIR" "$DROVER_RUNNER_HOOK_DIR"
rm -rf -- "$DROVER_RUNNER_DIR" "$DROVER_RUNNER_HOOK_DIR"
```

If deregistration cannot reach GitHub, remove the stale runner from the
repository runners page after the local service is stopped, then delete the
validated local directories.
