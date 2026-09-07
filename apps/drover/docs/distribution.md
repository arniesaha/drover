# iOS distribution configuration and privacy inventory

`Drover` is a self-hosted client. The iOS binary does not use analytics, ads,
tracking, or a publisher-operated notification service. It connects only to
the Drover server URL that the person configures or receives from a pairing QR
code. That server is the direct recipient of the app's authenticated traffic;
the server may in turn have its own configured model providers and retention
policy. Those host-side practices are not iOS-binary collection, but they must
be described accurately in the product privacy policy and App Store Connect.

## Privacy manifest inventory

`Drover/PrivacyInfo.xcprivacy` declares the following first-party collection
for `NSPrivacyCollectedDataTypePurposeAppFunctionality`. Each item is linked to
the configured Drover account/server credential, and none is used for tracking.

| Manifest data type | What the app sends or retains | Code evidence |
| --- | --- | --- |
| `Name` | The device name provided during QR pairing. | `Drover/Screens/Settings/PairingView.swift` calls `UIDevice.current.name` when it calls `DroverClient.pair`. |
| `Device ID` | The APNs device token uploaded to the configured server for notifications. | `Drover/PushRegistrar.swift` receives the APNs token and calls `registerAPNsToken`. |
| `Other User Content` | User-entered prompts, terminal input, and any clipboard text that the person explicitly pastes into a terminal. The server returns agent/session and terminal output for display. | `DroverKit/DroverClient.swift`, `MessageStream.swift`, `TerminalStream.swift`, and `Drover/Screens/Terminal/TerminalBridge.swift`. |
| `Photos or Videos` | Image data that the person explicitly selects in the chat composer or session-launch view. The app downscales it, sends it as a JPEG attachment to the configured Drover server, and that server can persist it with the session attachments. | `Drover/Screens/Chat/Composer.swift`, `Drover/Screens/Launch/LaunchView.swift`, and `DroverKit/DroverClient.swift`. |
| `Other Data` | The configured server URL, pairing code, API bearer credential, session IDs, and model choices needed to authenticate and operate the self-hosted client. | `DroverKit/ServerConfig.swift`, `Keychain.swift`, `DroverClient.swift`, and `HarnessModelCatalogStore.swift`. |

The pairing QR camera is used only to decode the pairing payload locally;
`PairingView.swift` does not save or upload camera frames. The bearer credential
is stored in the Keychain rather than `UserDefaults` and is sent only as the
`Authorization: Bearer` header to the selected Drover server. The app does not
access contacts, photos beyond images explicitly selected with `PhotosPicker`,
location, health, microphone, or advertising identifiers. The system-managed
picker provides the selected asset without broad photo-library authorization,
so the app has no photo-library usage-description key. It writes to the
pasteboard for explicit copy actions and reads pasteboard text only for an
explicit terminal-paste action.

The manifest uses the `UserDefaults` required-reason API declaration with
`CA92.1`. The app uses it solely for app-owned configuration and state: server
URL, display and terminal preferences, notification state, and a bounded model
catalog. It does not store the bearer credential there, and this declaration
does not authorize unrelated sharing of those values. Source inspection found
no file-timestamp, disk-space, boot-time, active-keyboard, or other
required-reason API use in first-party code.

The only directly declared external package is SwiftTerm 1.13.0, pinned in
`project.yml`. The Xcode 26.6 resolution also includes SwiftTerm's transitive
`swift-argument-parser` 1.8.2. Neither resolved dependency checkout contains a
privacy manifest; the built app therefore contains the first-party
`PrivacyInfo.xcprivacy` above. Recheck this on every dependency update. The app
manifest covers first-party collection and required-reason use; any dependency
manifest remains the dependency author's declaration.

## Transport and ATS

The user can currently configure an HTTPS or HTTP server URL. HTTPS maps to
`wss` for live streams; HTTP maps to `ws`. The supported matrix is:

| Endpoint | Current configuration | Security consequence |
| --- | --- | --- |
| HTTPS server | ATS default trust evaluation; `wss` streams. | Preferred. Bearer traffic is encrypted in transit. |
| Private-LAN HTTP address, unqualified name, or `.local` name | The existing `NSAllowsArbitraryLoads` exception permits the configurable HTTP path. | HTTP does not encrypt bearer traffic. Use HTTPS when the server supports it. |
| Tailscale IP or private tailnet hostname | The URL parser recognizes Tailscale addresses, but does not establish TLS itself. The broad ATS exception preserves existing HTTP Tailscale/custom-host use. | A private overlay address alone does not encrypt bearer traffic. Prefer HTTPS/WSS with a trusted certificate. |

`NSAllowsArbitraryLoads` is a deliberate temporary broad exception. It remains
because `ServerConfig` accepts arbitrary user-configured self-hosted HTTP
hosts, while no functional transport matrix has yet established a safe finite
set of private IP/CIDR and tailnet-domain exceptions. A local-network exception
can override the broad key on newer iOS releases and therefore is not added
until tests prove the complete accepted-host matrix remains functional. Before
public submission, the release owner must either validate and implement
narrower exceptions or give App Review the concrete self-hosted-server
justification required for this broad exception.

## Configurations and build inputs

`Debug` and `Release` retain `Drover/Drover.entitlements`, including
`aps-environment=development`. `StoreRelease` is a release configuration that
selects `Drover/Drover-AppStore.entitlements`, which changes only APNs to
`production`. `DroverAppStore` uses `StoreRelease` for its archive action.

`CFBundleShortVersionString` and `CFBundleVersion` come from
`MARKETING_VERSION` and `CURRENT_PROJECT_VERSION` in both the XcodeGen source
and the checked-in `Info.plist`. The defaults (`0.1.0` and `1`) are for
development. The release owner must choose the submission values from verified
App Store Connect state; for a given marketing version, the selected build
number must exceed every uploaded build. Archive tooling accepts those values
only as explicit inputs. It does not choose a candidate version, signing
identity, team, profile, or upload destination.

Generate and inspect configuration locally from the repository root:

```sh
(cd apps/drover && xcodegen generate)
xcodebuild -project apps/drover/Drover.xcodeproj -scheme Drover -configuration Debug -showBuildSettings
xcodebuild -project apps/drover/Drover.xcodeproj -scheme DroverAppStore -configuration StoreRelease -showBuildSettings
plutil -lint apps/drover/Drover/PrivacyInfo.xcprivacy
```

For a non-submission packaging check, use explicit values and inspect the
unsigned simulator product after the build:

```sh
xcodebuild -project apps/drover/Drover.xcodeproj -scheme DroverAppStore \
  -configuration StoreRelease -sdk iphonesimulator -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath /path/out/DroverD1 -jobs 2 \
  MARKETING_VERSION=0.4.5 CURRENT_PROJECT_VERSION=1 build
```

Those example values do not assert upload eligibility or select a release
candidate; they only demonstrate that build settings reach the product.

## Archive and signed-artifact verification

Use a fresh, approved version and build number. The output directory must be an
absolute path that does not already exist. The wrapper is portable and accepts
any such output parent. For this release program, the release owner retains
local candidates under the M2 artifact volume by setting the explicit path
below. The wrapper creates the archive directory, a zip of that archive, and
`archive-record.json`. The record binds the candidate to its commit,
clean-tree state, effective Xcode developer directory and iPhoneOS SDK,
version, build, and SHA-256 hash of the archive zip.

The archive command also needs an explicit private signing configuration. The
configuration is generated from the reviewed distribution identity, team, and
provisioning profile. It pins manual signing, the approved certificate common
name after an exact SHA-1 preflight, the team, the profile UUID, and the
temporary keychain path. Keep it outside the repository and do not print it.
The wrapper rejects an incomplete, ambient, or automatic signing configuration;
it never selects the first identity that a machine happens to have.

```sh
export DROVER_APP_VERSION="<approved-version>"
export DROVER_APP_BUILD="<approved-build>"
export DROVER_IOS_OUTPUT="/Volumes/M2 1/drover-data/ios-candidates/<candidate>"
export DROVER_IOS_SIGNING_CONFIG="<private-reviewed-signing-config>"

scripts/ios/archive.sh --version "$DROVER_APP_VERSION" \
  --build "$DROVER_APP_BUILD" --output "$DROVER_IOS_OUTPUT" \
  --signing-config "$DROVER_IOS_SIGNING_CONFIG"
```

`archive.sh` uses the effective `DEVELOPER_DIR` when one is set, otherwise the
selected Xcode. It requires Xcode 26 and iPhoneOS SDK 26.0 or later, generates
the project, and archives the `DroverAppStore` scheme with the supplied build
settings and private configuration. It records the working-tree state rather
than silently changing it. It does not pass a signing identity, team, profile,
or secret on a command line. Build and signing output stay in a temporary
directory and are deleted after the command; failures use short sanitized
messages.

The wrapper calls the verifier on the archive before it records a candidate.
The verifier may also be used on the application unpacked from a reviewed IPA:

```sh
scripts/ios/verify_archive.py --app "$DROVER_IOS_OUTPUT/Drover.xcarchive" \
  --expected-version "$DROVER_APP_VERSION" --expected-build "$DROVER_APP_BUILD"

ditto -x -k "$DROVER_EXPORTED_IPA" "$DROVER_EXPORTED_IPA_DIRECTORY"
scripts/ios/verify_archive.py \
  --app "$DROVER_EXPORTED_IPA_DIRECTORY/Payload/Drover.app" \
  --expected-version "$DROVER_APP_VERSION" --expected-build "$DROVER_APP_BUILD"
```

The verifier requires an iPhoneOS product and exact bundle identifier, version,
build, iPhoneOS SDK, and embedded privacy manifest. It invokes `codesign` to
verify the signed bundle, read its certificate authorities, and read its
signed entitlements. It requires an Apple Distribution authority, production
APNs, and `get-task-allow=false`. A simulator product, development APNs, a
development debugger entitlement, absent or malformed signing evidence,
unexpanded build settings, or a source entitlement file cannot make a
candidate pass. The source entitlement file is never used as signed evidence.

Before an authorized export or upload, validate both the archive and the
unpacked IPA with the same expected values. The release owner creates and
reviews the Xcode-generated App Store Connect export configuration, then
inspects the archive privacy report and dependency contents. The release owner
also verifies App Store Connect access and agreements, app identity, and
distribution provisioning. A working development installation is not evidence
that these distribution prerequisites are available.

## Internal TestFlight artifact chain

Use Python 3.11 or later and the selected Xcode 26.6 toolchain. For the internal
channel, archive with both `--channel testflight-internal` and
`--staging-url "$DROVER_TESTFLIGHT_STAGING_URL"` in addition to the explicit
version, build, output, and signing configuration above. The URL must be an
HTTPS origin without credentials, a path, query, or fragment. An optional root
slash, uppercase hostname, and explicit default port are normalized away.

The archive command sets `DROVER_TESTFLIGHT_STAGE_ONLY=YES`,
`DROVER_TESTFLIGHT_STAGING_URL`, and `DROVER_ALLOW_ARBITRARY_LOADS=NO`.
The signed Info.plist must contain the exact normalized origin, the literal
`YES` stage flag consumed by the app, and boolean
`NSAppTransportSecurity.NSAllowsArbitraryLoads=false`. These checks apply again
to the signed application unpacked from the exported IPA. They can be requested
directly with `verify_archive.py --expected-staging-url`. The archive record adds
`channel` and `staging_url_sha256`; it does not record the origin itself.

```sh
scripts/ios/export_ipa.sh \
  --archive "$DROVER_IOS_OUTPUT/Drover.xcarchive" \
  --output "$DROVER_IOS_EXPORT_OUTPUT" \
  --export-options "$DROVER_IOS_SIGNING_TEMP/ExportOptions.plist" \
  --version "$DROVER_APP_VERSION" --build "$DROVER_APP_BUILD" \
  --staging-url "$DROVER_TESTFLIGHT_STAGING_URL"
```

The export output directory must be absolute and absent. The export-options
path must live in an existing owner-only temporary signing directory. An
existing plist must also be owner-only and reviewed. If the file is absent,
the wrapper generates it from the verified archive's signing authority, signed
team entitlement, and decoded embedded provisioning profile UUID. It checks
the profile team and application identifier against the signed app. Keep the
generated plist inside that signing directory until signing cleanup removes it.

Export options require `method=app-store-connect`, `destination=export`,
`signingStyle=manual`, `testFlightInternalTestingOnly=true`, and
`manageAppVersionAndBuildNumber=false`. Xcode 26.6 documents these options in
`xcodebuild -help`. Export cannot implicitly upload, enable external distribution,
or change the candidate build number. Exactly one IPA and one Payload application
must be present, and signed-artifact verification must succeed before the output
directory is created. The retained files are `Drover.ipa` and
`export-record.json`. That record contains only `version`, `build`,
`bundle_identifier`, `ipa_sha256`, and `staging_url_sha256`. Raw Xcode diagnostics,
export sidecars, and the unpacked app remain temporary and are discarded.

After the release owner has authorized upload and supplied the temporary
App Store Connect key directory:

```sh
scripts/ios/upload_testflight.sh \
  --ipa "$DROVER_IOS_EXPORT_OUTPUT/Drover.ipa" \
  --api-key-id "$DROVER_ASC_KEY_ID" --api-issuer "$DROVER_ASC_ISSUER" \
  --private-keys-dir "$DROVER_ASC_PRIVATE_KEYS_DIR" \
  --record "$DROVER_IOS_UPLOAD_RECORD"
```

The supplied directory and `AuthKey_<id>.p8` must belong to the current user,
have no group/other permissions, and be actual directories/files rather than
symlinks. The receipt path must be absolute and absent. The wrapper sets
`API_PRIVATE_KEYS_DIR` and pins `altool`'s first `./private_keys` lookup to that
same directory from a private temporary working directory. Key contents are
never passed in command arguments. Raw JSON and stderr are captured in private
temporary files and discarded on success or failure. Only a zero exit code and
recognized positive upload confirmation without product errors produce a
receipt. The receipt contains only `upload_confirmed` and `ipa_sha256`.

The upload wrapper uses `xcrun altool --upload-app -f` with JSON output and
returns after upload confirmation. It does not wait for Apple processing.
Upload confirmation, Apple processing, tester availability, and physical-device
acceptance are separate checks; this receipt asserts only the first. The caller
owns removal of the supplied temporary key directory and signing workspace.
Never publish IPA/archive files, staging origins, keys, issuer IDs, generated
signing options, or raw tool output as workflow artifacts. Disable shell tracing
when invoking commands with protected arguments.

## Protected manual CI archive

### Internal TestFlight staging gate

`ios-testflight-internal.yml` accepts only an approved version and build. Both
jobs check `main` before entering their protected environment, check out the
dispatch SHA, and use GitHub-hosted runners with Python 3.13. The release owner
must configure required reviewers and main-only deployment branches on both
`ios-testflight-staging` and `ios-testflight-upload` before enabling a dispatch.
Keep these credentials environment-scoped, never repository-wide. This
repository change does not configure environments or authorize a live upload.

Set the repository variable `DROVER_TESTFLIGHT_STAGING_URL` to the reviewed
staging HTTPS origin; avoid environment overrides so both jobs use the same
origin. The staging environment receives only
`DROVER_TESTFLIGHT_PREFLIGHT_TOKEN`. Its Ubuntu job makes exactly four possible
GETs: `/release-identity`, `/readyz`, `/harness/hosts`, and `/harness`. It accepts
only a root HTTPS origin, refuses every redirect, bypasses ambient proxies,
uses a ten-second timeout per request, and checks the exact dispatch SHA,
staging role, readiness, online `testflight-staging-mac-mini`, an enabled known
structured runtime, and a matching successful probe no older than 30 minutes.
It never creates a probe session. Refresh the operator-run probe before dispatch.
The archive job downloads this run's sanitized record and checks its source SHA
and origin digest before signing, so an environment override or changed variable
cannot silently select a different staging endpoint.

Run the client with URL/token environment variables to keep the token out of
process arguments; explicit `--url` and `--token` remain available for callers
that manage their own argument exposure:

```sh
python3 scripts/testflight/verify_staging.py --expected-sha "$CANDIDATE_SHA" \
  --record "$PRIVATE_OUTPUT/preflight-record.json"
```

Failures emit only a fixed category. Successful records contain only source
SHA, package version, role, normalized probe completion timestamp, fixed host
ID, and SHA-256 of the normalized staging origin. Neither response bodies nor
session identifiers are retained.

The upload environment holds the seven distribution signing values documented
below and `DROVER_APPSTORE_API_KEY_ID`, `DROVER_APPSTORE_API_ISSUER_ID`, and
`DROVER_APPSTORE_API_PRIVATE_KEY_BASE64`. It receives no staging credential.
The macOS job selects Xcode 26.6, repeats the package, app unit, and deterministic
UI slices from `ios.yml`, archives the stage-only app, exports a verified internal
IPA, and confirms upload. The Apple key is materialized only at upload under
`$RUNNER_TEMP/private_keys/AuthKey_<id>.p8` with mode `0600` in a `0700` directory.

Export needs the temporary signing identity in the runner's user keychain
search list. The workflow snapshots that list privately before signing setup,
adds the temporary keychain, and restores the exact original list in `always()`
cleanup before deleting the temporary keychain/profile and both credential
directories. This reversible sequence is workflow configuration only; validate
it on the protected hosted runner before relying on a live export. No local or
fleet keychain changes are part of repository verification.

Only sanitized preflight/archive/export/upload JSON records become Actions
artifacts. Approval delay and the iOS checks can make the original probe older
by upload time; its 30-minute freshness is assessed when preflight runs.
Upload confirmation does not assert Apple processing, internal tester
availability, or physical-device acceptance. The workflow does not wait for
processing or assign external testers.

### Distribution archive

`.github/workflows/ios-distribution.yml` is dispatch-only and uses the
protected `ios-distribution` environment. Its job is restricted to `main`
before that environment is entered and checks out the exact `github.sha` that
GitHub reviewed for the dispatch. It does not run for pull requests or execute
a selected feature branch with distribution inputs.

Before enabling it, the release owner configures required reviewers and the
seven protected signing inputs named in the workflow: a base64 PKCS#12 bundle
and its password, a base64 provisioning profile, the approved team ID, profile
UUID, certificate SHA-1, and certificate common name. The setup script creates
a fresh temporary keychain and imports the PKCS#12 material without placing
its password in command arguments. It installs and verifies the exact profile,
checks the exact
certificate SHA-1 in that keychain, and writes the private manual signing
configuration. It removes the temporary keychain, profile, and configuration
after the archive step. A missing or mismatched prerequisite fails before an
archive is attempted.

The workflow reports its selected Xcode and SDK, runs the archive wrapper, and
uploads only sanitized metadata (`archive-record.json`). The signed archive,
its zip, exported IPA, and provisioning profile are not uploaded to GitHub
Actions because protected environments do not make Actions artifact downloads
private. Retain any signed candidate locally under the explicit root-owned
output path after the release owner has completed the signing evidence review.

The workflow selects `macos-26` and
`/Applications/Xcode_26.6.app/Contents/Developer`. That path and its iPhoneOS
26.5 SDK were listed in the current GitHub runner image documentation when this
workflow was added. GitHub updates runner images regularly, so a missing path
fails clearly instead of silently using another Xcode. Recheck the
[GitHub macOS 26 image inventory](https://github.com/actions/runner-images/blob/main/images/macos/macos-26-Readme.md)
and [Apple's submission requirements](https://developer.apple.com/app-store/submitting/)
when dispatching a candidate. Apple currently requires iOS uploads to use the
iOS 26 SDK or later; this does not raise the app's iOS 18.0 deployment target.
