# Chat Viewport Stability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep a bounded structured-chat transcript visible and pinned to its actual visual bottom while replies, folded tool results, keyboard changes, and navigation transitions update the screen.

**Architecture:** Preserve the existing transcript fold and raw-message row lookup, but expose a separate visual-tail identifier from the already-cached folded items. `ChatView` will eagerly render the bounded tail and use that visual-tail identifier for automatic and manual bottom scrolling; older-page anchor restoration remains raw-message based.

**Tech Stack:** Swift 6, SwiftUI, Observation, Swift Testing, XCTest/XCUITest, XcodeGen, Xcode 26.

## Global Constraints

- Initial history remains bounded to the newest 200 raw messages.
- Keep the keyboard open after a successful Send.
- Keep automatic scrolling unanimated, coalesced, and gated by the existing pinned state.
- Do not pull a user who has scrolled upward back to the tail.
- Preserve explicit older-page loading and raw-message anchor restoration.
- Do not change server APIs, pagination, persistence, or transcript folding.
- A failed send retains composer text and attachments; a successful send clears them.

---

### Task 1: Model the bottom-most visual transcript row

**Files:**
- Modify: `apps/drover/DroverKit/Sources/DroverKit/ChatModel.swift`
- Test: `apps/drover/DroverKit/Tests/DroverKitTests/ChatModelDerivedStateTests.swift`

**Interfaces:**
- Consumes: `ChatModel.items: [TranscriptItem]`, cached against `messagesVersion`.
- Produces: `ChatModel.visualTailRowID: String?`, equal to `items.last?.id`.
- Preserves: `ChatModel.latestRowID`, whose existing meaning is the row updated by the newest raw event.

- [ ] **Step 1: Write the failing model regression**

Add tool-action, status, and tool-result helpers to `ChatModelDerivedStateTests`, then add this test:

```swift
@Test func visualTailDoesNotMoveBackwardWhenAResultUpdatesAnEarlierStep() {
    let model = ChatModel.fixture()
    let action = HarnessMessage(
        seq: 1,
        type: .toolAction,
        role: "assistant",
        text: "Bash",
        payload: ["tool_use_id": .string("tool-1")]
    )
    let status = HarnessMessage(seq: 2, type: .status, text: "Working")
    let result = HarnessMessage(
        seq: 3,
        type: .toolResult,
        role: "tool",
        text: "ok",
        payload: ["tool_use_id": .string("tool-1")]
    )

    model.ingest(.message(action))
    model.ingest(.message(status))
    model.ingest(.message(result))

    #expect(model.latestRowID == action.id)
    #expect(model.visualTailRowID == status.id)

    let output = HarnessMessage(seq: 4, type: .assistantOutput, text: "Done")
    model.ingest(.message(output))
    #expect(model.visualTailRowID == output.id)
}
```

- [ ] **Step 2: Run the focused test and confirm RED**

Run:

```bash
swift test --package-path apps/drover/DroverKit --filter ChatModelDerivedStateTests.visualTailDoesNotMoveBackwardWhenAResultUpdatesAnEarlierStep
```

Expected: compilation fails because `ChatModel` has no member `visualTailRowID`.

- [ ] **Step 3: Add the minimal visual-tail property**

Add beside `latestRowID` in `ChatModel.swift`:

```swift
/// The bottom-most row in visual transcript order. A newly-arrived raw
/// event can update an earlier folded row, so pinned scrolling must use
/// this rather than `latestRowID`.
public var visualTailRowID: String? {
    items.last?.id
}
```

This deliberately reuses `itemsCache`; do not introduce a second transcript fold or another cache.

- [ ] **Step 4: Run the focused regression and derived-state suite**

Run:

```bash
swift test --package-path apps/drover/DroverKit --filter ChatModelDerivedStateTests
```

Expected: all `ChatModelDerivedStateTests` pass, including the new folded-result ordering case.

- [ ] **Step 5: Commit the model contract**

```bash
git add apps/drover/DroverKit/Sources/DroverKit/ChatModel.swift apps/drover/DroverKit/Tests/DroverKitTests/ChatModelDerivedStateTests.swift
git commit -m "fix(ios): track the visual transcript tail"
```

---

### Task 2: Stabilize the SwiftUI transcript viewport

**Files:**
- Modify: `apps/drover/Drover/Screens/Chat/ChatView.swift`
- Modify: `apps/drover/DroverUITests/E2EValidationUITests.swift`

**Interfaces:**
- Consumes: `ChatModel.visualTailRowID: String?` from Task 1.
- Preserves: `TranscriptItem.rowID(containing:in:)` for older-page anchor restoration.
- Produces: an eagerly materialized bounded transcript whose pinned-scroll destinations always resolve to its last visual row.

- [ ] **Step 1: Refine the live simulator regression**

Keep `testExistingLongChatDoesNotBlankAcrossSendAndReopen`, but make it diagnostic-safe and explicit:

```swift
XCTAssertTrue(waitForVisibleTranscript(in: app, minimumCount: 8, timeout: 30))
let beforeSend = visibleTranscriptCount(in: app)

composer.tap()
composer.typeText("Viewport stability diagnostic. Reply with exactly: STABLE")
app.buttons["composer-send"].tap()

for sample in 1...4 {
    Thread.sleep(forTimeInterval: 0.5)
    XCTAssertGreaterThan(visibleTranscriptCount(in: app), 0,
                         "transcript blanked after Send at sample \(sample)")
}
XCTAssertGreaterThan(beforeSend, 0)
```

After navigating back and reopening the same session, retain the assertion that at least eight transcript elements become visible. Keep the test opt-in through `DROVER_SMOKE_SESSION_ID`; do not hard-code a production session as the default.

- [ ] **Step 2: Run the current implementation once to preserve the reproduction evidence**

Run the existing live UI test with the generated project, simulator destination, local server URL, token supplied through the environment, and an explicit disposable or user-approved session ID.

Expected before the fix: screenshots/element samples may show the previously reproduced blanking or upward/downward viewport jump when a tool result attaches to an earlier row. The deterministic unit test from Task 1 is the required RED gate; this live test is supporting behavioral evidence.

- [ ] **Step 3: Eagerly materialize the bounded transcript**

In `ChatView.transcript`, replace:

```swift
LazyVStack(alignment: .leading, spacing: 8) {
```

with:

```swift
VStack(alignment: .leading, spacing: 8) {
```

Update adjacent comments so they describe an eager, bounded transcript and remove claims about late-measuring lazy rows.

- [ ] **Step 4: Route all bottom scrolling to the visual tail**

In both `scrollToBottomButton(_:)` and both passes of `scheduleScroll(with:)`, replace `model.latestRowID` with `model.visualTailRowID`:

```swift
guard let rowID = model.visualTailRowID else { return }
```

and:

```swift
guard !Task.isCancelled, isPinnedToBottom,
      let rowID = model.visualTailRowID else {
    pendingScroll = nil
    return
}
```

The second settle pass must also read `model.visualTailRowID`. Leave the `.onChange(of: model.messages.last?.id)` trigger intact so in-place updates still schedule a scroll, but the scroll destination never moves backward in visual order.

- [ ] **Step 5: Audit the SwiftUI source for stale bottom-scroll targets**

Run:

```bash
rg -n "LazyVStack|latestRowID|visualTailRowID|rowID\(containing" apps/drover/Drover/Screens/Chat/ChatView.swift
```

Expected: no `LazyVStack` or `latestRowID` remains in `ChatView`; all bottom-scroll paths use `visualTailRowID`; the older-history path still uses `rowID(containing:in:)`.

- [ ] **Step 6: Build and run the targeted simulator regression**

Regenerate the ignored project and build/test on the existing simulator:

```bash
cd apps/drover
xcodegen generate
xcodebuild test -project Drover.xcodeproj -scheme DroverUITests -destination 'id=E20AAAC7-9FA1-42D0-B135-D1E5C690B403' -derivedDataPath /private/tmp/drover-chat-reset-sim -only-testing:DroverUITests/E2EValidationUITests/testExistingLongChatDoesNotBlankAcrossSendAndReopen -resultBundlePath /private/tmp/drover-chat-reset-fixed.xcresult
```

Supply `TEST_RUNNER_DROVER_SMOKE_URL`, `TEST_RUNNER_DROVER_SMOKE_TOKEN`, and `TEST_RUNNER_DROVER_SMOKE_SESSION_ID` without printing the token. Expected: the test passes, every post-Send sample has visible transcript content, and reopening paints the transcript.

- [ ] **Step 7: Commit the viewport implementation and regression**

```bash
git add apps/drover/Drover/Screens/Chat/ChatView.swift apps/drover/DroverUITests/E2EValidationUITests.swift
git commit -m "fix(ios): keep chat viewport stable during updates"
```

---

### Task 3: Verify and deploy the candidate to the physical iPhone

**Files:**
- Verify: `apps/drover/DroverKit`
- Verify: `apps/drover/Drover.xcodeproj`
- No server files are modified.

**Interfaces:**
- Consumes: the model and view behavior from Tasks 1 and 2.
- Produces: a signed Release app installed and launched on the connected iPhone, plus recorded test/build evidence.

- [ ] **Step 1: Run the complete package test suite**

```bash
swift test --package-path apps/drover/DroverKit
```

Expected: all suites pass with zero failures.

- [ ] **Step 2: Run repository hygiene checks**

```bash
git diff --check
git status --short
```

Expected: no whitespace errors; only intentional committed changes are present. `Drover.xcodeproj` remains ignored and is not staged.

- [ ] **Step 3: Build a signed Release app for the connected iPhone**

Verify that Arnab's iPhone (`D3684608-B2AF-5EF5-B739-73B449A85C1E`) is available with `xcrun devicectl list devices`, then run:

```bash
xcodebuild -project apps/drover/Drover.xcodeproj -scheme Drover -configuration Release -destination 'generic/platform=iOS' -derivedDataPath /private/tmp/drover-chat-reset-device DEVELOPMENT_TEAM=DK2PC4RH5G CODE_SIGN_STYLE=Automatic -allowProvisioningUpdates build
```

Expected: `** BUILD SUCCEEDED **`, with a signed `Drover.app` under the derived-data Release products directory.

- [ ] **Step 4: Install and launch on the iPhone**

```bash
xcrun devicectl device install app --device D3684608-B2AF-5EF5-B739-73B449A85C1E /private/tmp/drover-chat-reset-device/Build/Products/Release-iphoneos/Drover.app
xcrun devicectl device process launch --device D3684608-B2AF-5EF5-B739-73B449A85C1E com.arnab.drover
```

Expected: installation and launch succeed. Open the known long structured session, send a wrapping reply, observe an intervening tool/status/result sequence, then navigate back and reopen. The transcript must not blank or jump upward.

- [ ] **Step 5: Confirm deployment scope**

```bash
git diff d73f960 --name-only
```

Expected: only iOS source/tests/docs are listed. Therefore no NAS or local Drover server restart is required.

- [ ] **Step 6: Report the candidate without merging unconfirmed behavior**

Summarize the exact tests, simulator result, signed build/install result, and deployment scope. Keep the fix branch isolated until physical-device behavior is confirmed; only then fast-forward local `main` if the user asks for or has already authorized that integration.
