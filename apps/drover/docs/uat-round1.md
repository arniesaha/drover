# iOS UAT round 1

Base: `2cd696c3ec5d570300954a3cbdc3f9386775d204` (main).

| Finding | Before | After |
| --- | --- | --- |
| F09 | Personal email addresses and home paths appeared in fixture and test sources, including Analytics. | Whole `apps/` email and home-path scan completed. Email values use `example.com`; home paths and project names use synthetic samples. Matching test expectations were updated. Bundle and service identifiers remain unchanged. |
| F01 | Recovery and approval actions shared narrow horizontal rows; session titles and metadata had fixed line limits. | Accessibility sizes use vertical action layouts with icons and labels. Approval and recovery actions have minimum 44-point targets. Session metadata stacks and text wraps fully at accessibility sizes. Retry banners also reflow. |
| F04 | The bubble claimed delivery was still being confirmed while the banner claimed confirmation failed. | Bubble and banner use the same pending-turn status, “Couldn’t confirm delivery”, with one Retry action. Retry retains the original idempotency identifier. Sending and recovered manual-review states retain their distinct meanings. |
| F02 | Pair was inside the scrolling content below the code field. | Settings pairing and onboarding pairing pin Pair in a bottom safe-area inset, above the keyboard. |
| F07 | Launch was a final form section below the fold. | Launch is pinned to the sheet bottom and stays reachable on iPhone and iPad. The working-directory field is untouched. |
| Fixture | Core journeys had only a short static transcript. | `long-streaming` supplies 281 paginated history events with 40 step groups, 120 tool calls and results, and an approval. A long assistant turn streams 80 chunks at 250 ms intervals, with a final complete answer of over 19,000 characters in one tall bubble. Shared data powers a SwiftUI preview and DEBUG fixture/demo launches through the existing transport. |

## Verification

All commands ran in foreground tool sessions, with logs captured under `/tmp`.

- Full iOS unit suite on iPad Pro 11-inch (M5), iOS 27: 76 XCTest tests and 972 Swift Testing tests passed.
- Fixture integration test: bounded cold open, older paging, step grouping, approval decoding, and all 80 timed live chunks through the real `MessageStream` passed. Full chunk count, sequence and answer length are checked.
- Accessibility UI suite on iPhone 18 Pro, iOS 27: all 4 tests passed, covering recovery controls at Accessibility XXXL, accessibility audit, streaming approval labels and tap sizes, and primary actions with keyboard.
- Observability UI suite on iPhone 18 Pro: all 6 tests passed with synthetic account labels.
- Primary-action UI test on iPad Pro 11-inch (M5): passed with the keyboard visible and Launch reachable on opening the sheet.
- iOS simulator build: passed.
- `git diff --check`: passed.
- Email scan: remaining email literals all use `example.com`, including the intentional uppercase normalization case. Home-path scan contains only synthetic home names or placeholders.

The first unit run exposed two stale test expectations for a sanitized encoded
path and the former delivery hint; both were corrected. Initial accessibility
navigation assumed short session rows; the test now scrolls to and taps the
visible part of fully wrapped rows. Approval and recovery screenshots are kept
as XCTest attachments for visual review.

## Scope and deferred items

No requested finding is deferred. Transcript scroll/follow code is untouched
for PR #565. Working-directory replacement remains in its separate PR. Physical
device UAT was not part of these simulator checks. No push, PR, merge or deploy
was performed.

## Files

- `apps/drover/Drover/DroverApp.swift`
- `apps/drover/Drover/Screens/Chat/ChatHintBanner.swift`
- `apps/drover/Drover/Screens/Chat/ChatView.swift`
- `apps/drover/Drover/Screens/Chat/DecisionBlock.swift`
- `apps/drover/Drover/Screens/Launch/LaunchView.swift`
- `apps/drover/Drover/Screens/Onboarding/OnboardingView.swift`
- `apps/drover/Drover/Screens/Sessions/SessionRow.swift`
- `apps/drover/Drover/Screens/Settings/PairingView.swift`
- `apps/drover/Drover/UITesting/FixtureHubURLProtocol.swift`
- `apps/drover/Drover/UITesting/FixtureScenarioData.swift`
- `apps/drover/Drover/UITesting/FixtureWebSocketConnector.swift`
- `apps/drover/Drover/UITesting/LongStreamingTranscriptFixture.swift`
- `apps/drover/Drover/UITesting/ObservabilityFixtureRoot.swift`
- `apps/drover/Drover/UITesting/UITestScenario.swift`
- `apps/drover/DroverKit/Sources/DroverKit/ChatModel.swift`
- `apps/drover/DroverKit/Sources/DroverKit/PathCompletion.swift`
- `apps/drover/DroverKit/Sources/DroverKit/SessionCardPresentation.swift`
- `apps/drover/DroverKit/Tests/DroverKitTests/ChatModelTests.swift`
- `apps/drover/DroverKit/Tests/DroverKitTests/CockpitPresentationTests.swift`
- `apps/drover/DroverKit/Tests/DroverKitTests/LaunchCwdCompletionTests.swift`
- `apps/drover/DroverKit/Tests/DroverKitTests/LaunchModelTests.swift`
- `apps/drover/DroverKit/Tests/DroverKitTests/ModelsTests.swift`
- `apps/drover/DroverKit/Tests/DroverKitTests/NotifierTests.swift`
- `apps/drover/DroverKit/Tests/DroverKitTests/SessionCardPresentationTests.swift`
- `apps/drover/DroverKit/Tests/DroverKitTests/Support/Fixtures.swift`
- `apps/drover/DroverKit/Tests/DroverKitTests/TerminalWireTests.swift`
- `apps/drover/DroverTests/InboxStatusHeaderLayoutTests.swift`
- `apps/drover/DroverTests/LongStreamingTranscriptFixtureTests.swift`
- `apps/drover/DroverTests/ProviderCapacityCardTests.swift`
- `apps/drover/DroverTests/SessionRowStaleLayoutTests.swift`
- `apps/drover/DroverUITests/AccessibilityJourneyUITests.swift`
- `apps/drover/DroverUITests/ObservabilityFixtureUITests.swift`
- `apps/drover/README.md`
- `apps/drover/docs/uat-round1.md`
