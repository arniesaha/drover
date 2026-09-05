# Drover for iOS

Drover is a native iOS client for supervising coding-agent sessions across your
machines. It connects directly to the `/harness` REST and WebSocket API exposed
by `drover-server`.

The app is distributed from source for v0.1. It is not available through the
App Store or TestFlight.

## Requirements

- Xcode 16 or newer
- An iOS 18 simulator or device
- [XcodeGen](https://github.com/yonaskolb/XcodeGen)
- A reachable `drover-server`

Install XcodeGen with Homebrew:

```bash
brew install xcodegen
```

## Generate And Build

```bash
cd apps/drover
xcodegen generate
xcodebuild -project Drover.xcodeproj -scheme Drover \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' \
  build
```

`Drover.xcodeproj` is generated from `project.yml` and is not committed. Run
`xcodegen generate` after changing the project definition or source layout.

To install on an iPhone, open the generated project in Xcode, select the
`Drover` scheme and your device, choose your development team under Signing &
Capabilities, and press Run. Builds signed with a free Apple developer account
must be installed again after their provisioning profile expires.

The same thing without the GUI, for when the change you are deploying came out
of a terminal in the first place:

```bash
scripts/deploy-ios.sh              # regenerate, build, install, launch
scripts/deploy-ios.sh --no-launch  # stop after installing
scripts/deploy-ios.sh --device iPad
```

It regenerates the project, resolves the first paired device matching the name,
signs with automatic provisioning, and installs over `devicectl`. Override the
team with `DROVER_TEAM_ID`. The device must be paired and unlocked.

## Connect

On a first computer, choose **Set Up Drover** and run the one install command it
shows. The installer starts the hub and local harness, then prints a pairing QR
code. Scan that code in **Pair & Connect**. If the camera is unavailable, use
**Or enter it by hand** and enter the server URL and pairing code from
`drover-server pair`.

After pairing, choose an authenticated supported agent and a project the agent
can read, then send a small task. `drover-server setup-check --host HOST
--harness HARNESS --project PROJECT` is an optional terminal-side diagnosis if
that path is not ready. It reports read-only recovery categories; add `--json`
when a structured result is useful.

Drover v0.1 is intended for trusted private networks. Do not expose the server
directly to the public internet. See [Security](../../docs/security.md) and
[Multi-host setup](../../docs/multi-host.md).

## Test

Run these commands from the repository root. Generate the project before
running an Xcode suite:

```bash
(cd apps/drover && xcodegen generate)
```

The `Drover` scheme selects the `DroverTests` target. Run it with an installed
iPhone simulator:

```bash
DROVER_SIMULATOR_ID="$(xcrun simctl list devices available | awk -F '[()]' '/iPhone/ {print $2; exit}')"
test -n "$DROVER_SIMULATOR_ID"
xcodebuild -project apps/drover/Drover.xcodeproj -scheme Drover \
  -destination "id=$DROVER_SIMULATOR_ID" \
  test
```

The `DroverKit` package has a separate test target. Run its full deterministic
suite directly so its recovery and bounded-work tests execute:

```bash
swift test --package-path apps/drover/DroverKit --jobs 2
```

The credential-free deterministic journey and its Accessibility XXXL companion
exercise the app's real navigation against the synthetic `core-journey`
fixture. They do not need a server URL, token, or network access:

```bash
DROVER_SIMULATOR_ID="$(xcrun simctl list devices available | awk -F '[()]' '/iPhone/ {print $2; exit}')"
test -n "$DROVER_SIMULATOR_ID"
xcodebuild -project apps/drover/Drover.xcodeproj -scheme DroverUITests \
  -destination "id=$DROVER_SIMULATOR_ID" \
  -only-testing:DroverUITests/DeterministicJourneyUITests \
  -only-testing:DroverUITests/AccessibilityJourneyUITests \
  test
```

Live UI checks stay opt-in. Run `E2EValidationUITests` only when a developer
has supplied a disposable live server and credential through local environment
variables. Pass them to the test runner without printing their values:

```bash
: "${DROVER_SMOKE_URL:?set a disposable live server URL}"
: "${DROVER_SMOKE_TOKEN:?set a disposable live-server token}"
DROVER_SIMULATOR_ID="$(xcrun simctl list devices available | awk -F '[()]' '/iPhone/ {print $2; exit}')"
test -n "$DROVER_SIMULATOR_ID"
xcodebuild -project apps/drover/Drover.xcodeproj -scheme DroverUITests \
  -destination "id=$DROVER_SIMULATOR_ID" \
  TEST_RUNNER_DROVER_SMOKE_URL="$DROVER_SMOKE_URL" \
  TEST_RUNNER_DROVER_SMOKE_TOKEN="$DROVER_SMOKE_TOKEN" \
  -only-testing:DroverUITests/E2EValidationUITests \
  test
```

`SettingsSmokeUITests` is also manual-only. Do not add either live class to
the required CI selection.

## Release-device evidence

Root records this evidence before release on the smallest supported physical
iPhone. Record the reference iPhone model and OS, app build, local network,
and data fixture. Exercise light and dark appearance, the largest Dynamic
Type size, VoiceOver, Reduce Motion, keyboard and paste, camera pairing,
background and foreground, a development-account notification tap, and a
long-code or diff session.

Measure cached-screen, latest-page, and send-acknowledgement behavior on that
device before comparing the results with the 1 s, 3 s, and 1.5 s targets.
Record an unavailable measurement as unavailable. These observations are
physical-device release evidence, not simulator timings or phone p95 claims.

## Project Layout

```text
apps/drover/
  project.yml       XcodeGen source of truth
  Drover/           SwiftUI app target
  DroverKit/        Client, models, streaming, and presentation package
  DroverTests/      App test target
  DroverUITests/    UI test target
```

`DroverKit` keeps the network and state model independent of SwiftUI. The app
target owns navigation, screens, notification handling, and terminal UI.
