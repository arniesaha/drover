# Internal TestFlight end-to-end runbook

Operator guide for the **first Internal TestFlight** upload of Drover.
Follow this document in order. It does not authorize a live upload by itself:
Apple agreements, certificates, and the App Store Connect app record must
already exist or be created by a human with the right access. The interactive
provisioner below handles the remaining GitHub, tunnel, and Mac staging setup.

This lane is **internal-only** (`testFlightInternalTestingOnly=true`). It is
not public TestFlight, not App Store submission, and not the development
`scripts/deploy-ios.sh` path.

Source of truth for automation:

| Piece | Path |
| --- | --- |
| Workflow | [`.github/workflows/ios-testflight-internal.yml`](../../../.github/workflows/ios-testflight-internal.yml) |
| Staging hub | [`deploy/testflight-staging/README.md`](../../../deploy/testflight-staging/README.md) |
| Artifact / signing detail | [`distribution.md`](distribution.md) |
| App identity | `PRODUCT_BUNDLE_IDENTIFIER=com.arnab.drover` in `apps/drover/project.yml` |

Optional local tool check (no secrets):
[`scripts/ios/verify_testflight_prereqs.sh`](../../../scripts/ios/verify_testflight_prereqs.sh).

## Interactive provisioning (recommended)

Run the provisioner on the staging Mac from a clean Drover checkout. It starts
with a read-only audit and requires typed confirmation before it changes
Cloudflare, GitHub, launchd, or staging state:

```sh
.venv/bin/python scripts/ios/provision_testflight.py audit --json

chmod 600 /private/path/distribution.p12 \
  /private/path/profile.mobileprovision \
  /private/path/AuthKey_<KEY_ID>.p8

.venv/bin/python scripts/ios/provision_testflight.py apply \
  --public-origin 'https://testflight-staging.example.com' \
  --p12 /private/path/distribution.p12 \
  --profile /private/path/profile.mobileprovision \
  --asc-key /private/path/AuthKey_<KEY_ID>.p8 \
  --asc-issuer-id '<ASC_ISSUER_UUID>'
```

The default `codex` staging harness opens an isolated Codex login under the
staging home. For a dedicated Anthropic API key file instead, add
`--harness claude-code --provider-credential /private/path/anthropic_api_key`.
The key file must also be mode `0600`.

Defaults and prompts:

- Source is the current checkout; candidate SHA is its fetched `origin/main`.
- GitHub repository is `arniesaha/drover`; the required reviewer defaults to
  the authenticated `gh` user.
- The ASC key ID is inferred from `AuthKey_<KEY_ID>.p8` when possible.
- The PKCS#12 password is read with a hidden prompt, never a CLI argument.
- Missing `cloudflared` can be installed with Homebrew only after typing
  `INSTALL`; first-time Cloudflare authorization opens its login flow.
- Each external phase requires a separate `YES` confirmation. Secret values
  are streamed through stdin and are not written to command arguments.

The provisioner validates the real certificate/profile/key, creates or updates
the protected GitHub Environments and `main` branch policies, creates the
dedicated locally managed Cloudflare tunnel, installs its launchd job, runs the
staging prepare/activate/probe sequence, and performs a final read-only audit.
It is safe to rerun with the same inputs.

It deliberately cannot create the Apple Distribution certificate/profile, ASC
API key, paid agreements, or the `com.arnab.drover` App Store Connect app
record. It pauses for an explicit confirmation that the app record and API-key
upload permission exist before making any external changes.

To explicitly dispatch after provisioning, append
`--dispatch --version '<version>' --build '<unused-build-number>'`. This adds a
fifth confirmation before starting the workflow. Omit it for setup-only use.

## 1. Prerequisites (human / Apple / machine)

Complete these before configuring GitHub or dispatching the workflow.

### Apple Developer and App Store Connect

- An Apple Developer Program team that can issue **Apple Distribution**
  certificates and App Store / TestFlight provisioning for
  `com.arnab.drover`.
- An App Store Connect **app record** for that bundle ID (create if missing).
- Paid agreements and banking/tax state current enough that ASC accepts uploads.
- An App Store Connect API key with permission to upload builds. You will store
  its Key ID, Issuer ID, and `.p8` private key as GitHub Environment secrets
  (names below). Do not commit the `.p8`.

### Signing material (for Environment `ios-testflight-upload`)

You need a reviewed **Apple Distribution** identity and an App Store Connect
provisioning profile matching `com.arnab.drover`. Encode them the same way
`scripts/ios/setup_distribution_signing.sh` expects:

| Secret (exact name) | Contents |
| --- | --- |
| `DROVER_DISTRIBUTION_P12_BASE64` | Base64 of the PKCS#12 (`.p12`) export |
| `DROVER_DISTRIBUTION_P12_PASSWORD` | Password for that `.p12` |
| `DROVER_DISTRIBUTION_PROFILE_BASE64` | Base64 of the `.mobileprovision` |
| `DROVER_DISTRIBUTION_TEAM_ID` | Ten-character team ID (`^[A-Z0-9]{10}$`) |
| `DROVER_DISTRIBUTION_PROFILE_UUID` | Profile UUID from the provision file |
| `DROVER_DISTRIBUTION_IDENTITY_SHA1` | 40-hex SHA-1 of the distribution certificate |
| `DROVER_DISTRIBUTION_IDENTITY_NAME` | Exact common name: `Apple Distribution: … (TEAM_ID)` |

The setup script fails closed if any of these are missing or mismatched. Do not
invent placeholder values in the repository.

Encode local files before pasting into GitHub Environment secrets
(`setup_distribution_signing.sh` only decodes). On macOS or Linux:

```sh
# PKCS#12, provisioning profile, and ASC .p8 (strip newlines for secret values)
base64 < path/to/distribution.p12 | tr -d '\n'
base64 < path/to/profile.mobileprovision | tr -d '\n'
base64 < path/to/AuthKey_<KEY_ID>.p8 | tr -d '\n'
```

Keep the plaintext files and encoded outputs out of the repository and chat logs.

### App Store Connect API (same Environment)

| Secret (exact name) | Contents |
| --- | --- |
| `DROVER_APPSTORE_API_KEY_ID` | ASC API Key ID (`[A-Z0-9]{8,20}`) |
| `DROVER_APPSTORE_API_ISSUER_ID` | ASC Issuer UUID |
| `DROVER_APPSTORE_API_PRIVATE_KEY_BASE64` | Base64 of the `.p8` key file bytes |

**Naming note:** local examples in [`distribution.md`](distribution.md) use
shell placeholders `DROVER_ASC_KEY_ID`, `DROVER_ASC_ISSUER`, and
`DROVER_ASC_PRIVATE_KEYS_DIR` for `upload_testflight.sh` CLI arguments. Those
are **not** GitHub secret names. CI materializes
`AuthKey_<DROVER_APPSTORE_API_KEY_ID>.p8` under `$RUNNER_TEMP/private_keys`
from the three `DROVER_APPSTORE_API_*` secrets above.

### Mac Mini staging hub

- A dedicated Mac Mini (or equivalent) for the isolated staging root described
  in [`deploy/testflight-staging/README.md`](../../../deploy/testflight-staging/README.md).
- Operator can run `scripts/testflight/stage.py` prepare / activate / probe /
  rollback against a clean checkout of the candidate SHA on `origin/main`.
- Dedicated Cloudflare tunnel (or equivalent) whose **only** ingress is
  `http://127.0.0.1:17080`. Port `17081` stays loopback.
- Staging provider credentials and APNs material live only under the staging
  home; never in this repo.
- The harness host ID the preflight gate expects is exactly
  `testflight-staging-mac-mini` (see `scripts/testflight/verify_staging.py`).

### Local operator workstation (optional checks)

- Python 3.11+ (CI uses 3.13; staging docs say 3.11+).
- For local archive/export experiments only: Xcode 26.6+, XcodeGen, and the
  same signing material -- not required merely to dispatch CI.
- `gh` CLI authenticated to this repository if you prefer dispatching from a
  terminal instead of the Actions UI.

## 2. Exact GitHub Environments, variables, and secrets

Copied from
[`.github/workflows/ios-testflight-internal.yml`](../../../.github/workflows/ios-testflight-internal.yml).
Keep credentials **environment-scoped**, never repository-wide.

### Repository variable (shared by both jobs)

| Kind | Exact name | Used by |
| --- | --- | --- |
| Variable (`vars.`) | `DROVER_TESTFLIGHT_STAGING_URL` | `preflight-staging` and `archive-upload` |

Set this as a **repository** variable to the reviewed HTTPS staging origin
(no credentials, path, query, or fragment). Avoid Environment-level overrides
so both jobs bind to the same origin. The archive job compares the preflight
record’s `staging_url_sha256` to this variable before signing.

### Environment `ios-testflight-staging`

Job: `preflight-staging` (`runs-on: ubuntu-latest`).

| Kind | Exact name |
| --- | --- |
| Secret | `DROVER_TESTFLIGHT_PREFLIGHT_TOKEN` |

This Environment receives **only** the preflight token. It must not hold
`DROVER_TESTFLIGHT_STAGING_URL` (repo variable only), distribution signing, or
App Store Connect API material.

### Environment `ios-testflight-upload`

Job: `archive-upload` (`runs-on: macos-26`).

| Kind | Exact name |
| --- | --- |
| Secret | `DROVER_DISTRIBUTION_P12_BASE64` |
| Secret | `DROVER_DISTRIBUTION_P12_PASSWORD` |
| Secret | `DROVER_DISTRIBUTION_PROFILE_BASE64` |
| Secret | `DROVER_DISTRIBUTION_TEAM_ID` |
| Secret | `DROVER_DISTRIBUTION_PROFILE_UUID` |
| Secret | `DROVER_DISTRIBUTION_IDENTITY_SHA1` |
| Secret | `DROVER_DISTRIBUTION_IDENTITY_NAME` |
| Secret | `DROVER_APPSTORE_API_KEY_ID` |
| Secret | `DROVER_APPSTORE_API_ISSUER_ID` |
| Secret | `DROVER_APPSTORE_API_PRIVATE_KEY_BASE64` |

This Environment receives **no** staging preflight token and no
`DROVER_TESTFLIGHT_STAGING_URL` (that remains a repository variable only).

### Related Environment (not this workflow)

`.github/workflows/ios-distribution.yml` uses Environment `ios-distribution`
with the same seven `DROVER_DISTRIBUTION_*` secrets for a signed archive
without TestFlight upload. Configure it separately; do not confuse it with
`ios-testflight-upload`.

## 3. Configure and protect the Environments

In the GitHub repository: **Settings → Environments**.

For **both** `ios-testflight-staging` and `ios-testflight-upload`:

1. Create the Environment if it does not exist (names must match exactly).
2. Enable **Required reviewers** (release owner / on-call).
3. Restrict **Deployment branches** to `main` only.
4. Add the secrets and confirm the repository variable
   `DROVER_TESTFLIGHT_STAGING_URL` is set.
5. Do not enable this workflow’s first live dispatch until reviewers and
   branch rules are in place. The interactive provisioner configures these;
   merely merging the repository changes does not.

Workflow gates (already in YAML):

- `if: github.ref == 'refs/heads/main'` on both jobs.
- `concurrency.group: ios-testflight-internal` with
  `cancel-in-progress: false`.
- Checkout uses `ref: ${{ github.sha }}` with `persist-credentials: false`.
- Permissions: `contents: read` only.

## 4. Staging hub steps (before every dispatch)

**Before prepare:** `RELEASE_SHA` must already be a full lowercase 40-hex commit
on the locally fetched `origin/main` (reachable from that ref). Fetch and review
that SHA first; `stage.py prepare` does not fetch or choose the candidate. The
workflow later rejects any dispatch that is not `refs/heads/main` at that SHA.

On the Mac Mini, follow
[`deploy/testflight-staging/README.md`](../../../deploy/testflight-staging/README.md)
for the rest of the isolated staging root.

Abbreviated operator sequence (placeholders only; never commit real values):

```sh
# Isolated home required by staging README (set before any command that uses it)
STAGING_HOME="$STAGING_ROOT/home"

# 1. Prepare (no services yet) -- only after RELEASE_SHA is on origin/main
python3 scripts/testflight/stage.py prepare \
  --repository "$REPOSITORY" --root "$STAGING_ROOT" \
  --sha "$RELEASE_SHA" --public-url "$STAGING_PUBLIC_ORIGIN"

# 2. Configure staging provider + APNs under $STAGING_HOME (see staging README)

# 3. Activate
python3 scripts/testflight/stage.py activate \
  --root "$STAGING_ROOT" --sha "$RELEASE_SHA"

# 4. Mint preflight credential (prints a secret -- keep private)
env -i HOME="$STAGING_HOME" PATH=/usr/bin:/bin \
  "$STAGING_ROOT/worktrees/$RELEASE_SHA/.venv/bin/drover-server" \
  --config "$STAGING_HOME/.drover/config.toml" \
  credentials issue-preflight --label internal-testflight

# Store that value as Environment secret DROVER_TESTFLIGHT_PREFLIGHT_TOKEN
# (or refresh it if rotated). Do not paste into issues, chat, or the repo.

# 5. Bounded structured probe (required; gate wants a probe ≤ 30 minutes old)
python3 scripts/testflight/stage.py probe \
  --root "$STAGING_ROOT" --sha "$RELEASE_SHA" --harness claude-code
```

Preflight (`scripts/testflight/verify_staging.py`) will then require:

- Exact dispatch SHA on `/release-identity`
- Staging role and `/readyz`
- Online host `testflight-staging-mac-mini` with an enabled `claude-code` or
  `codex` harness
- A matching successful probe no older than **30 minutes**
- HTTPS origin only; no redirects; ten-second timeouts

Refresh the probe immediately before dispatch if approval or queue delay may
exceed that window. The workflow never creates a probe session itself.

Optional operator-side dry check (uses the same env vars CI uses; keep the
token out of argv when possible):

```sh
export DROVER_TESTFLIGHT_STAGING_URL='https://your-staging-origin.example'
export DROVER_TESTFLIGHT_PREFLIGHT_TOKEN='…'   # private shell only
python3 scripts/testflight/verify_staging.py \
  --expected-sha "$RELEASE_SHA" \
  --record "$PRIVATE_OUTPUT/preflight-record.json"
```

## 5. Dispatch `ios-testflight-internal.yml`

1. Ensure the candidate commit is on `main` (the workflow rejects other refs).
2. Confirm staging probe freshness and Environment secrets.
3. Open **Actions → Internal TestFlight candidate → Run workflow**.
4. Inputs (required):

   | Input | Meaning |
   | --- | --- |
   | `version` | Approved `CFBundleShortVersionString` |
   | `build` | Approved `CFBundleVersion` (must exceed prior uploads for that marketing version in ASC) |

5. Approve Environment deployments when prompted (`ios-testflight-staging`,
   then `ios-testflight-upload` after preflight succeeds).

What the workflow does (do not re-implement ad hoc):

1. **preflight-staging** -- `verify_staging.py`; uploads sanitized
   `testflight-preflight-metadata`.
2. **archive-upload** -- binds candidate to preflight origin digest; installs
   XcodeGen; runs DroverKit tests, app unit tests, and deterministic UI
   journey slices; sets up distribution signing; archives with
   `--channel testflight-internal` and `--staging-url`; exports IPA with
   internal TestFlight options; uploads via `upload_testflight.sh`; cleans
   credentials in `always()`; uploads sanitized
   `testflight-candidate-metadata` when present.

Runner pin: `macos-26` with
`DEVELOPER_DIR=/Applications/Xcode_26.6.app/Contents/Developer`. If that path
is missing after a GitHub image change, the job fails loudly -- recheck the
[macOS 26 runner inventory](https://github.com/actions/runner-images/blob/main/images/macos/macos-26-Readme.md)
before retrying.

CLI alternative (same inputs; still requires Environment approvals):

```sh
gh workflow run ios-testflight-internal.yml \
  --ref main \
  -f version='<approved-version>' \
  -f build='<approved-build>'
```

## 6. Post-upload verification (Apple processing)

Upload confirmation is **only** the first gate. The workflow receipt asserts
`upload_confirmed` and `ipa_sha256`; it does **not** wait for Apple processing
or assign testers.

After a green `archive-upload` job:

1. Download the sanitized `testflight-candidate-metadata` artifact (if present)
   and confirm `upload-record.json` shows `upload_confirmed` without opening
   raw altool logs (those are discarded by design).
2. In App Store Connect → the Drover app → **TestFlight**:
   - Wait until the build appears and processing completes (can take minutes to
     hours; status is owned by Apple).
   - Confirm the build number and version match the dispatch inputs.
   - Confirm the build is eligible for **internal** testing only for this lane.
3. Confirm App Store Connect **users** (not external email invites) are on an
   **Internal** testing group for this build. Internal TestFlight is ASC-user
   Internal groups only; the workflow does not assign external testers.
4. Do not treat “workflow green” as “installable on a phone” until processing
   finishes and the build is available to that Internal group.

This repository change never claims a live upload succeeded.

## 7. Physical-device acceptance checklist

Install the internal build on the **smallest supported physical iPhone** used
for release evidence ([`apps/drover/README.md`](../README.md) -- Release-device
evidence). Record model, OS, app version/build, network, and fixture.

### Install and pair

Mint a short-lived pairing code against the **isolated staging hub** (same
`HOME` / `--config` isolation as the staging README). Do not use your personal
`~/.drover` home for this pass. Canonical pairing UX (QR scan, hand-entry
fallback, advertised URL) is in
[`docs/getting-started.md`](../../../docs/getting-started.md) (Connect The iOS
App); the command below is the staging-isolated equivalent of
`drover-server pair`:

```sh
STAGING_HOME="$STAGING_ROOT/home"
env -i HOME="$STAGING_HOME" PATH=/usr/bin:/bin \
  "$STAGING_ROOT/worktrees/$RELEASE_SHA/.venv/bin/drover-server" \
  --config "$STAGING_HOME/.drover/config.toml" \
  pair
```

Ensure `[server] advertised_url` in that staging config is the reviewed public
staging origin the app can reach (tunnel), then:

- [ ] Install from TestFlight (internal) on a physical device (not simulator).
- [ ] Pair to the **staging** hub with the QR or manual URL + pairing code from
      the mint above. Confirm the app uses the stage-only endpoint policy baked
      at archive time (`DROVER_TESTFLIGHT_STAGE_ONLY` / staging URL in
      Info.plist -- see `distribution.md`).
- [ ] Reject accidental pairing to a personal production hub for this
      acceptance pass unless that is an explicit separate check.

### Core flows

- [ ] Light and dark appearance.
- [ ] Largest Dynamic Type size.
- [ ] VoiceOver smoke on primary navigation.
- [ ] Reduce Motion.
- [ ] Keyboard and paste in terminal/chat as applicable.
- [ ] Camera pairing path (or documented hand-entry fallback).
- [ ] Background → foreground resume.
- [ ] Development-account notification tap if APNs is configured on staging.
- [ ] Long-code or diff session rendering.

### Perf targets (from app README)

On that physical device, measure and record (mark unavailable if not measured):

| Observation | Target |
| --- | --- |
| Cached-screen | 1 s |
| Latest-page | 3 s |
| Send-acknowledgement | 1.5 s |

These are physical-device release evidence, not simulator timings or phone p95
claims.

## 8. Rollback and failure handling

### Staging hub

```sh
python3 scripts/testflight/stage.py rollback \
  --root "$STAGING_ROOT" --sha "$PREVIOUS_SHA"
```

Re-run the probe for the rolled-back SHA before another dispatch. Rollback
does not revert database schemas or provider state -- choose a
schema-compatible candidate.

### Workflow / upload failures

| Symptom | What to do |
| --- | --- |
| Preflight fails (`stage_*` categories) | Fix staging: activate correct SHA, refresh probe, confirm host ID `testflight-staging-mac-mini`, confirm `DROVER_TESTFLIGHT_STAGING_URL` and token. Do not dispatch again until `verify_staging.py` passes locally. |
| Signing setup fails | Re-check the seven `DROVER_DISTRIBUTION_*` secrets against the real cert/profile (UUID, SHA-1, common name, team). |
| Archive / export / verify fails | Inspect sanitized records only; fix version/build, signing, or staging URL binding. Do not publish IPA/archive artifacts to GitHub. |
| Upload step fails | Confirm ASC API secrets, agreements, bundle ID, and that the build number is unused. Rotate the API key if exposure is suspected. |
| Workflow green but build missing in TestFlight | Wait for Apple processing; check ASC Activity / Processing. Re-upload only with a new build number after ASC rejects or processing fails permanently. |
| Processing fails in ASC | Read Apple’s processing error in ASC; fix signing/entitlements/ATS/privacy as indicated; bump build; re-stage probe; dispatch again from `main`. |
| Bad candidate already on internal TestFlight | Expire or stop testing that build in ASC; roll staging back if needed; ship a fixed build number. Do not “hotfix” by rewriting history on `main`. |

Credential cleanup is handled by the workflow’s `always()` step. If a job is
cancelled mid-flight, confirm in the run log that cleanup ran; rotate secrets
if a runner failure leaves uncertainty.

## 9. What this runbook deliberately does not do

- Invent or store Apple / GitHub secret values.
- Claim that an upload or processing step succeeded without ASC evidence.
- Create Apple credentials, agreements, or the App Store Connect app record.
- Cover public TestFlight, App Store review submission, or PyPI/server
  releases.
- Replace [`distribution.md`](distribution.md) deep dives on privacy, ATS, or
  local archive wrappers.

When in doubt, trust the workflow YAML and the staging README over memory.
