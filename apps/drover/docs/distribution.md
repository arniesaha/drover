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
| `Other Data` | The configured server URL, pairing code, API bearer credential, session IDs, and model choices needed to authenticate and operate the self-hosted client. | `DroverKit/ServerConfig.swift`, `Keychain.swift`, `DroverClient.swift`, and `HarnessModelCatalogStore.swift`. |

The pairing QR camera is used only to decode the pairing payload locally;
`PairingView.swift` does not save or upload camera frames. The bearer credential
is stored in the Keychain rather than `UserDefaults` and is sent only as the
`Authorization: Bearer` header to the selected Drover server. The app does not
read contacts, photos, location, health, microphone, or advertising identifiers.
It writes to the pasteboard for explicit copy actions and reads pasteboard text
only for an explicit terminal-paste action.

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
number must exceed every uploaded build. This task intentionally provides no
archive, signing, or upload command.

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
