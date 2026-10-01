# TestFlight runbook

Owner decision, 2026-09-30: there is no separate staging hub. TestFlight builds
use the production lane, which pairs with any valid hub (#390), and are
accepted against the operator's live hub. This path is retained for existing
links; the former internal staging lane and provisioner are retired.

Use [ios-testflight-production.yml](../../../.github/workflows/ios-testflight-production.yml)
from `main`. Signing and artifact details are in [distribution.md](distribution.md).
The production lane still exports with `testFlightInternalTestingOnly=true`:
Apple internal tester distribution is separate from the retired hub restriction.

## Prerequisites

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
| `DROVER_DISTRIBUTION_IDENTITY_NAME` | Exact common name: `Apple Distribution: ... (TEAM_ID)` |

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

## Protect the upload environment

Configure `ios-testflight-upload` with the signing and App Store Connect secrets
above, required reviewers, and main-only deployment branches. The workflow
checks out the dispatch SHA, uses `contents: read`, and serializes uploads with
`concurrency.group: ios-testflight-production`. The independent archive-only
workflow uses `ios-distribution`; it does not upload to TestFlight.

## Read-only live-hub smoke before every upload

Run this required manual gate immediately before production workflow dispatch
or an authorized local upload. CI has no configured live-hub URL/token; the
workflow does not run or enforce this manual gate. Queue or approval delays
require a fresh smoke before upload. Record the reviewed candidate SHA,
version/build and the smoke result privately with the release evidence.

Supply the live hub's root URL and an existing device or operator token through
`DROVER_TESTFLIGHT_HUB_URL` and `DROVER_TESTFLIGHT_HUB_TOKEN` in the environment.
Load the token from your private credential store; keep shell tracing disabled.
Do not pass it as an argument or paste it into release evidence. A preflight
scope token cannot access session listings and is unsuitable for this check.

```sh
# Set DROVER_TESTFLIGHT_HUB_TOKEN privately before running this command.
export DROVER_TESTFLIGHT_HUB_URL='https://hub.example.com'
python3 scripts/testflight/smoke_live_hub.py --budget-seconds 20
```

Successful output (exit 0):

```json
{"analytical": "ok", "checks": 4, "ok": true}
```

The [smoke script](../../../scripts/testflight/smoke_live_hub.py) makes only GETs:

| Route | Requirement |
| --- | --- |
| `/healthz` | HTTP 200, body `ok` (optional final newline) |
| `/readyz` | HTTP 200, JSON `ready: true` |
| `/harness/hosts` | Authenticated HTTP 200 within the shared budget |
| `/harness/sessions` | Authenticated HTTP 200 within the shared budget |

`/healthz` is liveness, not analytical readiness. Its `X-Drover-Analytical`
header is reported as `ok`, `recovering`, `failed-retrying`, or `unreported` for
missing/unknown values. `/readyz` must independently report ready. Empty host
or session listings are allowed; no fixed host ID, probe session or source SHA
is required. The operator checks actual capabilities in physical acceptance.

The default total wall budget is 20 seconds on macOS/Linux. Responses are
limited to 2 MiB; redirects and ambient proxies are refused. HTTP supports
private-network hubs; prefer HTTPS whenever available. No writes, pairing,
credential issuance, seeding, service restart or session creation occurs.
Failures exit 1 with a fixed category, for example
`live hub smoke failed: not_ready`. Tokens, URLs and response bodies are never
printed. Resolve failure and rerun before uploading.

## Dispatch and Apple processing

After the smoke passes and the release owner approves the candidate, open
**Actions → Production TestFlight candidate → Run workflow**, select `main`,
and supply the approved `version` and a previously unused `build` number.
Approve the `ios-testflight-upload` deployment when prompted.

The workflow selects Xcode 26.6 on `macos-26`, runs DroverKit tests, app unit
tests and deterministic/accessibility UI slices, sets up temporary signing,
archives with `--channel testflight-production`, exports with
`--unrestricted-hubs`, confirms upload and cleans temporary credentials in
`always()`. It retains only sanitized `testflight-production-candidate-metadata`.

After upload:

1. Confirm `upload-record.json` reports `upload_confirmed` and the expected IPA
   digest; archive/export records must match the approved version/build/SHA.
2. Wait for Apple processing in App Store Connect and verify version/build.
3. Assign the build to the intended internal ASC-user testing group and confirm
   it is installable. The workflow does not assign testers or wait for processing.
4. Complete the physical-device checklist below. A green upload is not acceptance.

## Physical-device acceptance on the live hub

Use the smallest supported physical iPhone used for release evidence, as
described in [the app README](../README.md). Record model, OS, app version/build,
network and test session privately. Keep hostnames, credentials and real session
content out of public release notes.

- [ ] Install the processed build from TestFlight on a physical device.
- [ ] Pair to the operator's live hub with its normal `drover-server pair` QR
      code or manual URL/code fallback; see [Getting Started](../../../docs/getting-started.md).
- [ ] Verify the selected hub and host, host listing, session listing and details.
- [ ] Run an operator-approved chat/terminal interaction and verify send/ack and
      rendering. These physical checks can write to the live hub; the smoke cannot.
- [ ] Verify light/dark appearance and largest Dynamic Type.
- [ ] Check VoiceOver primary navigation and Reduce Motion.
- [ ] Check keyboard/paste, camera pairing or hand-entry fallback.
- [ ] Check background/foreground resume and notification tap if APNs is configured.
- [ ] Check long-code or diff rendering.
- [ ] Sign out and re-pair; verify the intended connection is restored.

| Physical-device observation | Target |
| --- | --- |
| Cached-screen | 1 s |
| Latest-page | 3 s |
| Send-acknowledgement | 1.5 s |

Record measured results or mark unavailable. Simulator timings are not physical
release evidence. Acceptance is against the live hub; the app remains free to
pair to any valid hub.

## Failure handling

If smoke fails, fix the live hub or credentials and rerun; do not seed or restart
it as part of the check. For signing/export/upload errors, correct the protected
inputs using [distribution.md](distribution.md). If Apple processing fails,
inspect the ASC status and issue a corrected build number. For a bad installed
candidate, expire or stop testing it in ASC and ship a fixed build. No staging
rollback or live-hub mutation is part of this lane's smoke check.
