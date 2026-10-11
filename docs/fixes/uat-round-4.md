# UAT round 4

Base: `72fe3c8` (current main at task start). Local commit only.

## Findings

| Finding | Before | After |
| --- | --- | --- |
| F07 | Fleet totals and session rows separately filtered attention states. | Both use `SessionBuckets` against the session snapshot. Needs-you and finished membership have one source of truth. |
| F08 | Interrupt appeared for idle sessions and could send an idle request. | Menu and model require event-backed running activity, including a pending approval. Idle and completed turns cannot interrupt. |
| F09 | Composer padding and safe areas could expose transcript content. Analytics navigation was translucent. | Solid theme backgrounds cover the composer dock, top safe area and Analytics/History navigation bars. |
| F10 | History rendered any nonempty raw event. | Only user messages and assistant conversation output appear, labelled `You` and `Assistant` even when legacy records omit the role. Internal statuses, tools, raw events and thinking are hidden; internal-only histories show the readable-message empty state. |
| F11 | History footer used awkward wording, date range said `1 days`, and interface copy contained em dashes. | Footer says `1 session · All loaded`; range says `1 day`; app and package interface literals use middots. |
| F12 | Claude block fan-out repeated provider message usage under different event IDs. | Preserve provider message ID and aggregate once per message, retaining highest counters across partial blocks and replay. Known input: five block events formerly summed to 7,784 tokens; corrected total is 3,350 (300 fresh input, 3,000 cache reads, 50 output). Database rollup stays idempotent. |
| F16 | Recent folder labels used the last path component, exposing internal worktree IDs. | Prefer the matching session's repo name, scoped to the selected host. Conventional worktrees use the parent repo name; managed paths without repo metadata say `Project worktree`. Original paths remain selection values. |
| F06 / r2 F05 | No registered pairing URL handler. | Register `drover://`, strictly validate the payload and open a prefilled review sheet. No pairing request until explicit confirmation naming the destination server. Cancel sends nothing; QR cancellation permits another scan. Linked entry does not start the camera. |
| F13/F14/F15 | Analytics filter controls competed for horizontal space; full token values could wrap; composer chips crowded action buttons. | Analytics filters wrap, totals fit one line and labels grow vertically. Composer text scales with Dynamic Type, preferences wrap above actions at accessibility sizes, and oversized composers can scroll within a bounded dock. Largest-size placeholder remains fully visible. |

## Verification

- DroverKit full suite: 930 passed, 0 failed (893 Swift Testing tests and 37 XCTest tests).
- iOS app scheme: 1,093 passed, 0 failed (1,015 Swift Testing tests and 78 XCTest tests). This scheme includes package tests; these counts are not independent unique-test totals.
- Python normalization, Claude adapter and usage rollup: 54 passed, 0 failed.
- UI target: 4 passed, 0 failed. Largest accessibility size checks for Analytics filters, metrics and scrolling, plus composer action bounds and non-overlap, in light and dark mode. Screenshots retained in the test result bundle.
- iOS Simulator build and test build succeeded with Xcode 27 / iOS 27 on an iPhone simulator. The app test build covers the final changes; UI test builds cover the layout changes.
- `git diff --check` passed. No em dashes remain in app/package interface string literals.

Commands used:

```sh
swift test --package-path apps/drover/DroverKit --jobs 2
uv run --with pytest pytest -q tests/test_harness_usage_normalization.py tests/test_structured_claude.py tests/test_usage_rollup.py
xcodebuild -project apps/drover/Drover.xcodeproj -scheme Drover -destination 'platform=iOS Simulator,name=iPhone 18 Pro' test
```

UI selection: `AccessibilityJourneyUITests/testComposerActionsFitAtLargestTextSize`, its `InDarkMode` counterpart, `ObservabilityFixtureUITests/testAnalyticsFiltersAndMetricsAtLargestTextSize`, and its `InDarkMode` counterpart, through scheme `DroverUITests`.

## Deferred evidence and limits

- Historical Claude events without provider message IDs retain event-level deduplication. Repairing their existing totals requires reimport from a source carrying provider message IDs; this task does not mutate live hub data or guess message identity from internal ID prefixes.
- When managed folder recents have no repo metadata, the label is `Project worktree` rather than an inferred repo name.
- Physical-device link opening, live-server pairing, and another live-hub UAT pass remain external validation. Parser, URL registration and prefill are covered locally; no real credentials or pairing requests were used for validation.
- Layout evidence covers synthetic simulator fixtures, including Accessibility XXXL in both appearances. It does not claim physical-device testing or every possible session payload.
- No push, PR, merge or deployment.

## Files

- `apps/drover/Drover/DroverApp.swift`
- `apps/drover/Drover/Info.plist`
- `apps/drover/Drover/Screens/Auth/HarnessAuthSheet.swift`
- `apps/drover/Drover/Screens/Chat/ChatView.swift`
- `apps/drover/Drover/Screens/Chat/Composer.swift`
- `apps/drover/Drover/Screens/Chat/StepRunCard.swift`
- `apps/drover/Drover/Screens/Cockpit/AnalyticsView.swift`
- `apps/drover/Drover/Screens/Cockpit/DistributionSectionView.swift`
- `apps/drover/Drover/Screens/History/HistoryView.swift`
- `apps/drover/Drover/Screens/Launch/FolderBrowserSheet.swift`
- `apps/drover/Drover/Screens/Launch/LaunchView.swift`
- `apps/drover/Drover/Screens/Sessions/FleetHeader.swift`
- `apps/drover/Drover/Screens/Settings/PairingView.swift`
- `apps/drover/Drover/Screens/Shared/GlassPromptSurface.swift`
- `apps/drover/Drover/Screens/Shared/HarnessPreferenceControls.swift`
- `apps/drover/Drover/Screens/Terminal/TerminalView.swift`
- `apps/drover/DroverKit/Sources/DroverKit/ChatModel.swift`
- `apps/drover/DroverKit/Sources/DroverKit/DroverClient.swift`
- `apps/drover/DroverKit/Sources/DroverKit/FleetSummaryPresentation.swift`
- `apps/drover/DroverKit/Sources/DroverKit/FolderBrowserModel.swift`
- `apps/drover/DroverKit/Sources/DroverKit/LaunchModel.swift`
- `apps/drover/DroverKit/Sources/DroverKit/Notifier.swift`
- `apps/drover/DroverKit/Sources/DroverKit/PairingPayload.swift`
- `apps/drover/DroverKit/Sources/DroverKit/SessionEventSummary.swift`
- `apps/drover/DroverKit/Sources/DroverKit/SessionHistory.swift`
- `apps/drover/DroverKit/Sources/DroverKit/SessionStore.swift`
- `apps/drover/DroverKit/Tests/DroverKitTests/ChatModelTests.swift`
- `apps/drover/DroverKit/Tests/DroverKitTests/FleetSummaryPresentationTests.swift`
- `apps/drover/DroverKit/Tests/DroverKitTests/HarnessCapabilitiesTests.swift`
- `apps/drover/DroverKit/Tests/DroverKitTests/LaunchCwdCompletionTests.swift`
- `apps/drover/DroverKit/Tests/DroverKitTests/LaunchModelTests.swift`
- `apps/drover/DroverKit/Tests/DroverKitTests/NotifierTests.swift`
- `apps/drover/DroverKit/Tests/DroverKitTests/SessionEventSummaryTests.swift`
- `apps/drover/DroverKit/Tests/DroverKitTests/StepRunTests.swift`
- `apps/drover/DroverKit/Tests/DroverKitTests/StoreTests.swift`
- `apps/drover/DroverKit/Tests/DroverKitTests/UATRemainingTests.swift`
- `apps/drover/DroverTests/DroverColorTests.swift`
- `apps/drover/DroverTests/PairingViewTests.swift`
- `apps/drover/DroverUITests/AccessibilityJourneyUITests.swift`
- `apps/drover/DroverUITests/ObservabilityFixtureUITests.swift`
- `apps/drover/project.yml`
- `docs/fixes/uat-round-4.md`
- `src/drover/server/harness/structured/claude.py`
- `src/drover/server/harness/usage.py`
- `tests/test_harness_usage_normalization.py`
- `tests/test_structured_claude.py`
- `tests/test_usage_rollup.py`
