# Drover

Native iOS client for Nexus's harness API: browse sessions across hosts, see
which ones need you, launch new structured sessions, chat with them, and
attach a real terminal over the WebSocket proxy when you need one. Talks to
`nexus-server` (default port 7080) over the `/harness` REST + WebSocket
surface described in the repo-root `README.md`.

- App target: `Drover` (`com.arnab.drover`), iOS 18.0+.
- Unit tests: `DroverTests` is the xcodegen bundle target, but it sources
  `NexusKitTests` only — i.e. it covers `NexusKit` (client/model logic), not
  app-target code. `AppEnvironment`, `BackgroundRefresh`, and the SwiftUI
  screens have no unit test target; they're verified live (simulator run)
  instead. Run via the `Drover` scheme.
- UI tests: `DroverUITests`, run via the separate `DroverUITests` scheme.
- Local Swift package: `NexusKit` (the `NexusClient` actor + models, no UI).
- Remote dependency: `SwiftTerm` (terminal emulation for the Terminal tab).

## Prerequisites

- Xcode 16+ with an iOS 18 simulator runtime installed.
- [XcodeGen](https://github.com/yonaskolb/XcodeGen) (`brew install xcodegen`).
  The `.xcodeproj` is generated from `project.yml` and is not committed —
  you must run `xcodegen generate` before the first build and again any time
  `project.yml` or the source file layout changes.
- A running `nexus-server` reachable from wherever you build/run (simulator
  reaches `localhost`/LAN directly; a physical device needs the server
  reachable over Tailscale or your LAN — see the Tailscale note below).

## Build

```bash
cd apps/drover
xcodegen generate
```

This regenerates `Drover.xcodeproj`. Re-run it whenever `project.yml`
changes or you add/remove/rename source files (XcodeGen globs the source
tree; stale project references are the most common "file not found" build
error here).

## Run tests

Unit tests (`DroverTests` + `NexusKitTests`, no live server required — all
network calls are mocked via `MockURLProtocol`):

```bash
xcodebuild -project Drover.xcodeproj -scheme Drover \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' \
  test
```

Expect `** TEST SUCCEEDED **`. As of this writing that's 74 tests in 1 suite.

UI tests (`DroverUITests`) use a separate scheme, since they exercise a
different test target:

```bash
xcodebuild -project Drover.xcodeproj -scheme DroverUITests \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' \
  test
```

UI tests here are self-contained (no live server) unless you've added a
temporary smoke test of your own — see "Debug env override" below for how
those are typically driven.

## Run in the simulator

```bash
xcodebuild -project Drover.xcodeproj -scheme Drover \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' \
  build
xcrun simctl install booted \
  ~/Library/Developer/Xcode/DerivedData/Drover-*/Build/Products/Debug-iphonesimulator/Drover.app
xcrun simctl launch booted com.arnab.drover
```

On first launch you'll land on the Settings/onboarding screen and need to
enter a server URL and token by hand (see "Configuring the server" below),
unless you use the debug override.

### Debug env override (DEBUG builds only)

For local iteration you can skip manual onboarding by launching with two
environment variables. This path only exists in DEBUG builds —
`NexusKit`'s `ServerConfig.debugOverride()` is compiled out entirely in
Release (`#if DEBUG` / `#else return nil #endif`), so it can never affect a
device-installed Release build:

- `DROVER_BASE_URL` — e.g. `http://192.168.1.149:7080`
- `DROVER_TOKEN` — the bearer token, e.g. `$(cat ~/.nexus/api_token)`

`xcrun simctl launch` has no `--setenv` flag. To inject environment
variables into a simulator-launched app you must prefix them with
`SIMCTL_CHILD_` on the launch command itself:

```bash
SIMCTL_CHILD_DROVER_BASE_URL="http://192.168.1.149:7080" \
SIMCTL_CHILD_DROVER_TOKEN="$(cat ~/.nexus/api_token)" \
xcrun simctl launch --terminate-running-process booted com.arnab.drover
```

Note this only works for a plain `simctl launch` of an already-installed
app — an XCUITest runner process does not inherit these variables even when
they're set as `TEST_RUNNER_*` / `SIMCTL_CHILD_*` on the test invocation
itself, so UI tests that need a live, authenticated app should attach to an
externally-launched instance (`XCUIApplication().activate()`) rather than
launching the app themselves (`app.launch()`).

Never commit a real token, and never pass it via `-setenv`-style flags that
end up in shell history you might paste elsewhere — prefer `$(cat
~/.nexus/api_token)` inline as shown above.

## Install on a physical device

1. Open `apps/drover/Drover.xcodeproj` in Xcode (after `xcodegen generate`).
2. Select the `Drover` scheme and your iPhone as the run destination.
3. In the target's Signing & Capabilities tab, select your personal
   (free) Apple ID team. `CODE_SIGN_STYLE` is `Automatic`, so Xcode
   provisions automatically once a team is selected.
4. Press Run.

**Weekly re-sign**: a free (non-paid) Apple Developer account issues
provisioning profiles that expire after 7 days. After that window the app
simply stops launching on the device with a "untrusted developer" /
expired-profile failure — there's no push mechanism, no warning in advance.
The fix is always the same: reconnect the device to Xcode and press Run
again to re-sign and reinstall. There's nothing to configure to avoid this
short of a paid Apple Developer Program membership (90-day certs) or an
Enterprise/TestFlight distribution, both out of scope here.

### Configuring the server (device or simulator, non-debug path)

In the app's Settings screen:

1. Server URL — your Mac's Tailscale IP and port, e.g. `100.x.y.z:7080`
   (see Tailscale note below), or a plain LAN IP/port for same-network use.
2. Token — paste the contents of `~/.nexus/api_token` from the machine
   running `nexus-server`.
3. Tap "Test & Save". Allow notifications when prompted (needed for
   background "needs you" alerts).

## Tailscale note

Drover has no concept of pairing codes or discovery — it just needs an
HTTP(S) URL it can reach. Installing [Tailscale](https://tailscale.com) on
both the Mac running `nexus-server` and the iPhone gives you a stable,
private URL (the Mac's Tailscale IP) that works over cellular from
anywhere, without opening any ports on your home network. Note the Mac's
Tailscale IP (`tailscale ip -4`) and use `http://<that-ip>:7080` as the
server URL in Settings.

## Project layout

```
apps/drover/
  project.yml              # XcodeGen spec — source of truth for the .xcodeproj
  Drover/                   # App target: screens, models, app entry point
  DroverUITests/            # UI test target (separate scheme)
  NexusKit/                 # Local SPM package: NexusClient + models
  NexusKitTests/            # NexusKit unit tests (part of the Drover scheme)
```
