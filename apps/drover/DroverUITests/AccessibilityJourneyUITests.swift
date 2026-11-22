import Foundation
import XCTest

/// Exercises the real DEBUG-only core-journey route at an accessibility text
/// size. The scenario's fixture transport and state are owned by the app, so
/// this class deliberately drives Root, the fleet row, and Chat rather than
/// substituting any view or network double.
final class AccessibilityJourneyUITests: XCTestCase {
    private let timeout: TimeInterval = 5

    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    @MainActor
    func testComposerActionsFitAtLargestTextSize() {
        checkComposerLargestText(dark: false)
    }

    @MainActor
    func testComposerActionsFitAtLargestTextSizeInDarkMode() {
        checkComposerLargestText(dark: true)
    }

    @MainActor
    private func checkComposerLargestText(dark: Bool) {
        let app = coreJourneyApp()
        app.launchEnvironment["DROVER_UI_TEST_CHAT_PREFERENCES"] = "1"
        if dark { app.launchArguments += ["-drover.appearance", "dark"] }
        app.launch()
        openFixtureChat(in: app)
        let send = app.buttons["composer-send"]
        let attach = app.buttons["composer-attach"]
        let scroll = app.scrollViews["composer-scroll"]
        if scroll.exists && !send.isHittable { scroll.swipeUp() }
        XCTAssertTrue(send.waitForExistence(timeout: timeout))
        XCTAssertTrue(send.isHittable)
        XCTAssertTrue(attach.isHittable)
        XCTAssertGreaterThanOrEqual(send.frame.height, 44)
        XCTAssertGreaterThanOrEqual(attach.frame.height, 44)
        XCTAssertTrue(app.frame.contains(send.frame))
        XCTAssertTrue(app.frame.contains(attach.frame))
        XCTAssertFalse(send.frame.intersects(attach.frame))
        let model = app.buttons["composer-model"]
        let effort = app.buttons["composer-effort"]
        XCTAssertTrue(model.exists)
        XCTAssertTrue(effort.exists)
        // Preferences can wrap below the first visible row at XXXL. Scroll
        // within the composer until the model choice is a real hit target.
        for _ in 0..<3 where !model.isHittable { scroll.swipeUp() }
        XCTAssertTrue(model.isHittable)
        XCTAssertTrue(app.frame.contains(model.frame))
        // At XXXL the preferences wrap inside the scrollable input. Reach
        // the second chip there while Send stays outside that scroll view.
        for _ in 0..<3 where !effort.isHittable { scroll.swipeUp() }
        XCTAssertTrue(effort.isHittable)
        XCTAssertTrue(app.frame.contains(effort.frame))
        XCTAssertFalse(model.frame.intersects(effort.frame))
        XCTAssertTrue(send.isHittable)
        XCTAssertTrue(attach.isHittable)
        let screenshot = XCTAttachment(screenshot: app.screenshot())
        screenshot.name = dark ? "Composer largest text dark" : "Composer largest text light"
        screenshot.lifetime = .keepAlways
        add(screenshot)
    }

    @MainActor
    func testCompactActivityInLightMode() {
        checkCompactActivity(dark: false, largestText: false)
    }

    @MainActor
    func testCompactActivityAtLargestTextSizeInDarkMode() {
        checkCompactActivity(dark: true, largestText: true)
    }

    @MainActor
    private func checkCompactActivity(dark: Bool, largestText: Bool) {
        for state in ["active", "completed"] {
            let app = coreJourneyApp()
            if !largestText { app.launchArguments = [] }
            if dark { app.launchArguments += ["-drover.appearance", "dark"] }
            app.launchEnvironment["DROVER_UI_TEST_CHAT_ACTIVITY"] = state
            app.launch()
            openFixtureChat(in: app)
            let activity = app.descendants(matching: .any)["chat-activity"].firstMatch
            XCTAssertTrue(activity.waitForExistence(timeout: timeout))
            let title = state == "completed" ? "Turn complete" : "Preparing"
            if largestText {
                XCTAssertTrue(activity.label.contains(title))
            } else {
                XCTAssertTrue(app.staticTexts[title].exists)
            }
            if largestText || state == "completed" {
                // Large labels may need the available width to wrap.
                XCTAssertLessThanOrEqual(activity.frame.width, app.frame.width - (state == "completed" ? 28 : 32))
            } else {
                XCTAssertLessThan(activity.frame.width, app.frame.width * 0.9)
            }
            XCTAssertTrue(app.frame.contains(activity.frame))
            XCTAssertLessThanOrEqual(activity.frame.maxY, app.textFields["composer-input"].frame.minY)
            XCTAssertTrue(app.buttons["composer-send"].isHittable)
            let status = app.buttons["chat-floating-status"]
            if state == "active" {
                XCTAssertTrue(status.exists)
                XCTAssertGreaterThanOrEqual(status.frame.height, 44)
                XCTAssertLessThanOrEqual(status.frame.maxY, activity.frame.minY)
                status.tap()
                XCTAssertTrue(app.buttons["session-events-row"].waitForExistence(timeout: timeout))
                app.buttons["Done"].tap()
            } else {
                XCTAssertFalse(status.exists)
                XCTAssertTrue(app.buttons["session-events-row"].exists)
            }
            let screenshot = XCTAttachment(screenshot: app.screenshot())
            screenshot.name = "Compact activity \(state) \(dark ? "dark XXXL" : "light standard")"
            screenshot.lifetime = .keepAlways
            add(screenshot)
            app.terminate()
        }
    }

    @MainActor
    func testDetachedRunningChatChromeAtLargestTextSizeInLightMode() {
        checkDetachedRunningChatChrome(dark: false)
    }

    @MainActor
    func testDetachedRunningChatChromeAtLargestTextSizeInDarkMode() {
        checkDetachedRunningChatChrome(dark: true)
    }

    @MainActor
    private func checkDetachedRunningChatChrome(dark: Bool) {
        let app = coreJourneyApp()
        app.launchEnvironment["DROVER_UI_TEST_CHAT_ACTIVITY"] = "active"
        app.launchEnvironment["DROVER_UI_TEST_CHAT_PREFERENCES"] = "1"
        app.launchEnvironment["DROVER_UI_TEST_TRANSCRIPT_STREAM"] = "1"
        if dark { app.launchArguments += ["-drover.appearance", "dark"] }
        app.launch()
        openFixtureChat(in: app)
        let transcript = app.scrollViews.firstMatch
        transcript.swipeDown()
        transcript.swipeDown()
        let jump = app.buttons["chat-scroll-to-bottom"]
        XCTAssertTrue(jump.waitForExistence(timeout: timeout))
        let activity = app.buttons["chat-activity"]
        let inputScroll = app.scrollViews["composer-scroll"]
        let send = app.buttons["composer-send"]
        XCTAssertTrue(activity.exists)
        XCTAssertTrue(inputScroll.exists)
        XCTAssertLessThanOrEqual(jump.frame.width, 100)
        XCTAssertLessThanOrEqual(jump.frame.height, 70)
        XCTAssertGreaterThanOrEqual(jump.frame.height, 44)
        XCTAssertEqual(jump.label, "Jump to latest")
        XCTAssertLessThanOrEqual(activity.frame.height, 130)
        XCTAssertLessThanOrEqual(send.frame.maxY - inputScroll.frame.minY, 170)
        XCTAssertLessThanOrEqual(jump.frame.maxY, activity.frame.minY)
        XCTAssertLessThanOrEqual(activity.frame.maxY, inputScroll.frame.minY)
        XCTAssertTrue(jump.isHittable)
        XCTAssertTrue(app.buttons["composer-send"].isHittable)
        XCTAssertTrue(app.buttons["composer-attach"].isHittable)
        let screenshot = XCTAttachment(screenshot: app.screenshot())
        screenshot.name = "Detached running chat XXXL \(dark ? "dark" : "light")"
        screenshot.lifetime = .keepAlways
        add(screenshot)
        activity.tap()
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "Elapsed time:")).firstMatch
            .waitForExistence(timeout: timeout))
        app.buttons["Done"].tap()
        XCTAssertTrue(app.buttons["composer-send"].isHittable)
        app.textFields["composer-input"].tap()
        XCTAssertTrue(app.keyboards.firstMatch.waitForExistence(timeout: timeout))
        XCTAssertTrue(send.isHittable)
        XCTAssertTrue(jump.isHittable)
        XCTAssertGreaterThanOrEqual(jump.frame.minY, app.navigationBars.firstMatch.frame.maxY)
        XCTAssertLessThanOrEqual(send.frame.maxY, app.keyboards.firstMatch.frame.minY)
        XCTAssertLessThanOrEqual(jump.frame.maxY, activity.frame.minY)
        let keyboardScreenshot = XCTAttachment(screenshot: app.screenshot())
        keyboardScreenshot.name = "Floating running chrome with keyboard XXXL \(dark ? "dark" : "light")"
        keyboardScreenshot.lifetime = .keepAlways
        add(keyboardScreenshot)
        app.buttons["keyboard-dismiss"].tap()
        jump.tap()
        XCTAssertTrue(jump.waitForNonExistence(timeout: timeout))
    }

    @MainActor
    func testFloatingChromeWithReducedTransparency() {
        let app = coreJourneyApp()
        app.launchEnvironment["DROVER_UI_TEST_CHAT_ACTIVITY"] = "active"
        app.launchEnvironment["DROVER_UI_TEST_TRANSCRIPT_STREAM"] = "1"
        app.launchEnvironment["DROVER_UI_TEST_REDUCE_TRANSPARENCY"] = "1"
        app.launch()
        openFixtureChat(in: app)
        app.scrollViews.firstMatch.swipeDown()
        let jump = app.buttons["chat-scroll-to-bottom"]
        XCTAssertTrue(jump.waitForExistence(timeout: timeout))
        XCTAssertTrue(jump.isHittable)
        XCTAssertTrue(app.buttons["composer-send"].isHittable)
        let screenshot = XCTAttachment(screenshot: app.screenshot())
        screenshot.name = "Opaque floating chrome with Reduce Transparency XXXL"
        screenshot.lifetime = .keepAlways
        add(screenshot)
    }

    @MainActor
    func testFloatingArtifactsOpenAndTranscriptExtendsBehindComposer() {
        let app = coreJourneyApp()
        app.launchEnvironment["DROVER_UI_TEST_CHAT_ACTIVITY"] = "active"
        app.launchEnvironment["DROVER_UI_TEST_CHAT_ARTIFACTS"] = "1"
        app.launchEnvironment["DROVER_UI_TEST_TRANSCRIPT_STREAM"] = "1"
        app.launch()
        openFixtureChat(in: app)
        let chip = app.buttons["chat-floating-artifacts"]
        XCTAssertTrue(chip.waitForExistence(timeout: timeout))
        XCTAssertGreaterThanOrEqual(chip.frame.height, 44)
        XCTAssertTrue(chip.isHittable)
        let transcript = app.scrollViews.firstMatch
        let send = app.buttons["composer-send"]
        XCTAssertGreaterThan(transcript.frame.maxY, send.frame.maxY)
        XCTAssertLessThanOrEqual(chip.frame.maxY,
                                 app.buttons["chat-activity"].frame.minY)
        let inline = app.buttons["artifact-pane-toggle"]
        XCTAssertTrue(inline.exists)
        let jump = app.buttons["chat-scroll-to-bottom"]
        if jump.exists { jump.tap() }
        // Frame predicates do not reliably refresh a previously-resolved
        // XCUIElement while SwiftUI is settling a large transcript. Re-query
        // both semantic controls on every predicate evaluation, preserving
        // the same geometric assertion and five-second budget.
        let cleared = XCTNSPredicateExpectation(
            predicate: NSPredicate { _, _ in
                let currentInline = app.buttons["artifact-pane-toggle"]
                let currentChip = app.buttons["chat-floating-artifacts"]
                return currentInline.exists && currentChip.exists
                    && currentInline.frame.maxY <= currentChip.frame.minY
            }, object: app)
        XCTAssertEqual(XCTWaiter.wait(for: [cleared], timeout: timeout), .completed)
        chip.tap()
        XCTAssertTrue(app.buttons["Open Pull request"].waitForExistence(timeout: timeout))
        app.buttons["Done"].tap()
        XCTAssertTrue(chip.isHittable)
        let screenshot = XCTAttachment(screenshot: app.screenshot())
        screenshot.name = "Floating artifact and activity above composer XXXL"
        screenshot.lifetime = .keepAlways
        add(screenshot)
    }

    @MainActor
    func testPreferencesShareOneRowAtStandardTextSize() {
        let app = coreJourneyApp()
        app.launchEnvironment["DROVER_UI_TEST_CHAT_PREFERENCES"] = "1"
        app.launchArguments = []
        app.launch()
        openFixtureChat(in: app)
        let model = app.buttons["composer-model"]
        let effort = app.buttons["composer-effort"]
        XCTAssertTrue(model.waitForExistence(timeout: timeout))
        XCTAssertTrue(effort.exists)
        XCTAssertEqual(model.frame.midY, effort.frame.midY, accuracy: 1)
        XCTAssertLessThanOrEqual(model.frame.maxX, effort.frame.minX)
        XCTAssertTrue(app.buttons["composer-send"].isHittable)
        let screenshot = XCTAttachment(screenshot: app.screenshot())
        screenshot.name = "Composer preferences on one row"
        screenshot.lifetime = .keepAlways
        add(screenshot)
    }

    @MainActor
    func testWrappingDraftKeepsFocusAndSendReachableAtAccessibilityXXXL() {
        let app = coreJourneyApp()
        app.launch()
        openFixtureChat(in: app)

        let composer = app.textFields["composer-input"]
        let draft = "A wrapping accessibility draft keeps every word while the input grows and the model preferences remain available for review."
        composer.tap()
        composer.typeText(draft)

        XCTAssertEqual(composer.value as? String, draft, "wrapping must not replace the focused input")
        XCTAssertTrue(app.keyboards.firstMatch.exists, "wrapping must preserve keyboard focus")
        let send = app.buttons["composer-send"]
        XCTAssertTrue(send.isHittable, "Send must stay reachable while the draft wraps")
        XCTAssertTrue(app.frame.contains(send.frame))
        XCTAssertLessThanOrEqual(send.frame.maxY, app.keyboards.firstMatch.frame.minY)
        send.tap()
        XCTAssertTrue(app.staticTexts["chat-delivery-awaiting"].waitForExistence(timeout: timeout))
    }

    @MainActor
    func testHomeHeaderScrollsAwayAtAccessibilityXXXL() {
        let app = coreJourneyApp()
        app.launch()
        let fleet = app.scrollViews["fleet-list"]
        XCTAssertTrue(fleet.waitForExistence(timeout: timeout))
        XCTAssertGreaterThan(fleet.frame.height, app.frame.height * 0.5)
        let header = app.descendants(matching: .any)["home-status-header"].firstMatch
        XCTAssertTrue(header.waitForExistence(timeout: timeout))
        let initialY = header.frame.minY
        fleet.swipeUp()
        XCTAssertTrue(!header.exists || !header.isHittable || header.frame.minY < initialY - 40,
                      "The status header must move with the list at XXXL")
        let screenshot = XCTAttachment(screenshot: app.screenshot())
        screenshot.name = "Home scrolling header at Accessibility XXXL"
        screenshot.lifetime = .keepAlways
        add(screenshot)
    }

    @MainActor
    func testHomeBottomSessionClearsFloatingChromeAtAccessibilityXXXL() {
        let app = coreJourneyApp()
        app.launch()
        let fleet = app.scrollViews["fleet-list"]
        XCTAssertTrue(fleet.waitForExistence(timeout: timeout))

        let before = XCTAttachment(screenshot: app.screenshot())
        before.name = "Home bottom session before XXXL clearance"
        before.lifetime = .keepAlways
        add(before)

        // The last fixture item exercises the long-list boundary, where the
        // floating New Session control and tab chrome are closest together.
        let session = app.buttons["fixture-other-session"]
        let action = app.staticTexts["session-action-fixture-other-session"]
        XCTAssertTrue(action.waitForExistence(timeout: timeout))
        let viewport = scrollElementFullyIntoClearFleetViewport(action, in: app)
        XCTAssertTrue(session.exists)
        XCTAssertTrue(session.isHittable)
        XCTAssertTrue(
            viewport.contains(action.frame),
            "The session action label must scroll clear of floating chrome at XXXL"
        )
        XCTAssertTrue(session.label.contains("Fixture other journey"))

        let after = XCTAttachment(screenshot: app.screenshot())
        after.name = "Home bottom session clear of chrome XXXL"
        after.lifetime = .keepAlways
        add(after)

        let point = app.coordinate(withNormalizedOffset: .zero)
            .withOffset(CGVector(dx: action.frame.midX - app.frame.minX,
                                 dy: action.frame.midY - app.frame.minY))
        point.tap()
        XCTAssertTrue(
            app.textFields["composer-input"].waitForExistence(timeout: timeout),
            "The clear card hit region must open the bottom running session"
        )
    }

    @MainActor
    func testCoreJourneyKeepsRecoveryControlsReachableAtAccessibilityXXXL() {
        let app = coreJourneyApp()
        app.launch()

        openFixtureChat(in: app)
        sendFixtureMessage(in: app)

        XCTAssertTrue(
            app.staticTexts["chat-delivery-awaiting"].waitForExistence(timeout: timeout),
            "the fixture send should remain unconfirmed before relaunch"
        )

        app.terminate()
        app.launch()

        showRecoveryControls(in: app)

        let checkDelivery = app.buttons["chat-check-delivery"]
        XCTAssertTrue(checkDelivery.isHittable, "Check delivery should remain reachable at XXXL")
        XCTAssertTrue(
            app.buttons["chat-copy-pending-to-draft"].isHittable,
            "Copy to draft should remain reachable at XXXL"
        )
        XCTAssertTrue(
            app.buttons["chat-discard-pending"].isHittable,
            "Discard locally should remain reachable at XXXL"
        )

        XCTAssertEqual(checkDelivery.label, "Check delivery")
        XCTAssertEqual(app.buttons["chat-copy-pending-to-draft"].label, "Copy to draft")
        XCTAssertLessThanOrEqual(checkDelivery.frame.maxY, app.buttons["chat-copy-pending-to-draft"].frame.minY)
        let screenshot = XCTAttachment(screenshot: app.screenshot())
        screenshot.name = "Chat recovery at Accessibility XXXL"
        screenshot.lifetime = .keepAlways
        add(screenshot)
        checkDelivery.tap()
        let receiptCount = app.staticTexts["fixture-turn-receipt-count"]
        XCTAssertTrue(receiptCount.waitForExistence(timeout: timeout))
        XCTAssertEqual(receiptCount.label, "1", "checking delivery must not create a second turn")
        XCTAssertFalse(app.staticTexts["chat-delivery-manual-review"].exists)
    }

    @MainActor
    func testStreamingApprovalLabelsAtLargestTextSize() {
        let app = coreJourneyApp()
        app.launchEnvironment["DROVER_UI_TEST_SCENARIO"] = "long-streaming"
        app.launch()
        openSession(in: app)
        let allow = app.buttons["approval-allow"]
        let deny = app.buttons["approval-deny"]
        XCTAssertTrue(allow.waitForExistence(timeout: 10))
        XCTAssertEqual(allow.label, "Allow once")
        XCTAssertEqual(deny.label, "Deny")
        XCTAssertTrue(allow.isHittable)
        XCTAssertTrue(deny.isHittable)
        XCTAssertGreaterThanOrEqual(allow.frame.height, 44)
        XCTAssertGreaterThanOrEqual(deny.frame.height, 44)
        XCTAssertLessThanOrEqual(deny.frame.maxY, allow.frame.minY)
        let screenshot = XCTAttachment(screenshot: app.screenshot())
        screenshot.name = "Chat approval at Accessibility XXXL"
        screenshot.lifetime = .keepAlways
        add(screenshot)
        let chunk = app.staticTexts.containing(NSPredicate(format: "label CONTAINS %@", "chunk 3 of 80")).firstMatch
        XCTAssertTrue(chunk.waitForExistence(timeout: 10))
    }

    @MainActor
    func testCoreJourneyRecoveryControlsPassAccessibilityAudit() throws {
        let app = coreJourneyApp()
        app.launch()

        openFixtureChat(in: app)
        sendFixtureMessage(in: app)
        XCTAssertTrue(app.staticTexts["chat-delivery-awaiting"].waitForExistence(timeout: timeout))

        app.terminate()
        app.launch()
        showRecoveryControls(in: app)

        try app.performAccessibilityAudit(for: [
            .elementDetection,
            .hitRegion,
            .sufficientElementDescription,
        ])
    }

    @MainActor
    func testPrimaryActionsStayReachableWithKeyboard() {
        let app = coreJourneyApp()
        app.launchArguments = []
        app.launch()
        let settings = app.tabBars.buttons["Settings"]
        XCTAssertTrue(settings.waitForExistence(timeout: timeout))
        XCTAssertTrue(settings.isHittable)
        settings.tap()
        let scan = app.buttons["Scan pairing code"]
        XCTAssertTrue(scan.waitForExistence(timeout: timeout))
        scan.tap()
        let code = app.textFields["K7QP-2M4X"]
        for _ in 0..<4 where !code.isHittable { app.swipeUp() }
        XCTAssertTrue(code.waitForExistence(timeout: timeout))
        code.tap()
        code.typeText("SAMPLE-CODE")
        let pair = app.buttons["pairing-submit"]
        XCTAssertTrue(app.keyboards.firstMatch.exists)
        XCTAssertTrue(pair.isHittable)
        XCTAssertLessThanOrEqual(pair.frame.maxY, app.keyboards.firstMatch.frame.minY)
        app.terminate()
        app.launch()
        let launch = app.buttons["launch-button"]
        XCTAssertTrue(launch.waitForExistence(timeout: timeout))
        launch.tap()
        let confirm = app.buttons["launch-confirm-button"]
        XCTAssertTrue(confirm.waitForExistence(timeout: timeout))
        XCTAssertFalse(confirm.isEnabled)
        // Launch requires an explicitly chosen working directory (#608).
        let folder = app.buttons["launch-folder-browser"]
        XCTAssertTrue(folder.waitForExistence(timeout: 5))
        folder.tap()
        let home = app.buttons["Home, folder"]
        XCTAssertTrue(home.waitForExistence(timeout: 5))
        home.tap()
        let project = app.buttons["project, Git repository, folder"]
        XCTAssertTrue(project.waitForExistence(timeout: 5))
        project.tap()
        let useFolder = app.buttons["folder-browser-use"]
        XCTAssertTrue(useFolder.waitForExistence(timeout: 5))
        XCTAssertTrue(useFolder.isEnabled)
        useFolder.tap()
        XCTAssertTrue(folder.waitForExistence(timeout: 5))
        XCTAssertEqual(folder.value as? String, "/fixture/project")
        let prompt = app.textFields["composer-input"]
        XCTAssertTrue(prompt.waitForExistence(timeout: timeout))
        prompt.tap()
        prompt.typeText("fixture launch")
        XCTAssertTrue(app.keyboards.firstMatch.exists)
        XCTAssertTrue(confirm.isHittable)
        XCTAssertTrue(confirm.isEnabled)
        XCTAssertTrue(app.frame.contains(confirm.frame))
        XCTAssertLessThanOrEqual(confirm.frame.maxY, app.keyboards.firstMatch.frame.minY)
    }

    @MainActor
    private func coreJourneyApp() -> XCUIApplication {
        let app = XCUIApplication()
        app.launchEnvironment["DROVER_UI_TEST_SCENARIO"] = "core-journey"
        app.launchEnvironment["DROVER_UI_TEST_RUN_ID"] = UUID().uuidString
        app.launchArguments += [
            "-UIPreferredContentSizeCategoryName",
            "UICTContentSizeCategoryAccessibilityXXXL",
        ]
        return app
    }

    @MainActor
    private func openSession(in app: XCUIApplication) {
        let fleet = app.scrollViews["fleet-list"]
        XCTAssertTrue(fleet.waitForExistence(timeout: timeout))
        let session = app.buttons["fixture-session"]
        let viewport = scrollSessionIntoClearFleetViewport(session, in: app)
        if session.exists {
            let visible = session.frame.intersection(viewport)
            if !visible.isNull, visible.height >= min(44, session.frame.height),
               visible.width >= 44, session.isHittable {
                let point = app.coordinate(withNormalizedOffset: .zero)
                    .withOffset(CGVector(dx: visible.midX - app.frame.minX,
                                         dy: visible.midY - app.frame.minY))
                point.tap()
                return
            }
        }
        let screenshot = XCTAttachment(screenshot: app.screenshot())
        screenshot.name = "Unreachable XXXL fleet session"
        screenshot.lifetime = .keepAlways
        add(screenshot)
        XCTFail("the wrapped sample session should be reachable at XXXL; fleet=\(fleet.frame), session=\(session.frame), hittable=\(session.isHittable)")
    }

    @MainActor
    private func scrollSessionIntoClearFleetViewport(
        _ session: XCUIElement,
        in app: XCUIApplication
    ) -> CGRect {
        let fleet = app.scrollViews["fleet-list"]
        var viewport = clearFleetViewport(fleet, in: app)
        for _ in 0..<12 {
            viewport = clearFleetViewport(fleet, in: app)
            if session.exists {
                let visible = session.frame.intersection(viewport)
                if !visible.isNull, visible.height >= min(44, session.frame.height),
                   visible.width >= 44, session.isHittable {
                    return viewport
                }
            }
            guard !viewport.isNull, viewport.width >= 44, viewport.height >= 44 else { break }
            dragFleet(in: viewport, upward: true, app: app)
        }
        return clearFleetViewport(fleet, in: app)
    }

    @MainActor
    private func scrollElementFullyIntoClearFleetViewport(
        _ element: XCUIElement,
        in app: XCUIApplication
    ) -> CGRect {
        let fleet = app.scrollViews["fleet-list"]
        var viewport = clearFleetViewport(fleet, in: app)
        for _ in 0..<12 {
            viewport = clearFleetViewport(fleet, in: app)
            if element.exists, viewport.contains(element.frame) { return viewport }
            guard !viewport.isNull, viewport.width >= 44, viewport.height >= 44 else { break }
            let upward = !element.exists || element.frame.maxY > viewport.maxY
            dragFleet(in: viewport, upward: upward, app: app)
        }
        return clearFleetViewport(fleet, in: app)
    }

    @MainActor
    private func dragFleet(in viewport: CGRect, upward: Bool, app: XCUIApplication) {
        let origin = app.coordinate(withNormalizedOffset: .zero)
        let lower = origin.withOffset(CGVector(
            dx: viewport.midX - app.frame.minX,
            dy: viewport.minY + viewport.height * 0.8 - app.frame.minY
        ))
        let upper = origin.withOffset(CGVector(
            dx: viewport.midX - app.frame.minX,
            dy: viewport.minY + viewport.height * 0.2 - app.frame.minY
        ))
        (upward ? lower : upper).press(
            forDuration: 0.05,
            thenDragTo: upward ? upper : lower,
            withVelocity: .slow,
            thenHoldForDuration: 0.3
        )
    }

    @MainActor
    private func clearFleetViewport(
        _ fleet: XCUIElement,
        in app: XCUIApplication
    ) -> CGRect {
        // Both taps and scroll gestures must avoid floating controls. On iOS
        // 18 the fleet accessibility frame includes their hit region.
        var viewport = fleet.frame.intersection(app.frame)
        let launch = app.buttons["launch-button"]
        if launch.exists, viewport.intersects(launch.frame) {
            viewport.size.height = max(0, launch.frame.minY - viewport.minY - 8)
        }
        let tabBar = app.tabBars.firstMatch
        if tabBar.exists, viewport.intersects(tabBar.frame) {
            viewport.size.height = max(0, tabBar.frame.minY - viewport.minY - 8)
        }
        return viewport
    }

    @MainActor
    private func openFixtureChat(in app: XCUIApplication) {
        openSession(in: app)

        let recap = app.staticTexts["chat-recap-title"]
        let primarySessionLoaded = XCTNSPredicateExpectation(
            predicate: NSPredicate(format: "label == %@", "Fixture core journey"),
            object: recap
        )
        XCTAssertEqual(XCTWaiter.wait(for: [primarySessionLoaded], timeout: timeout), .completed)

        let composer = app.textFields["composer-input"]
        XCTAssertTrue(composer.waitForExistence(timeout: timeout))
        XCTAssertTrue(composer.isHittable, "the composer should remain reachable at XXXL")
    }

    @MainActor
    private func sendFixtureMessage(in app: XCUIApplication) {
        let composer = app.textFields["composer-input"]
        composer.tap()
        composer.typeText("fixture accessibility message")

        let send = app.buttons["composer-send"]
        XCTAssertTrue(send.waitForExistence(timeout: timeout))
        XCTAssertTrue(send.isHittable, "the send control should remain reachable at XXXL")
        send.tap()
    }

    @MainActor
    private func showRecoveryControls(in app: XCUIApplication) {
        openFixtureChat(in: app)
        XCTAssertTrue(
            app.staticTexts["chat-delivery-manual-review"].waitForExistence(timeout: timeout),
            "relaunch should expose the recovered delivery review state"
        )
        XCTAssertTrue(app.buttons["chat-check-delivery"].waitForExistence(timeout: timeout))
        XCTAssertTrue(app.buttons["chat-copy-pending-to-draft"].waitForExistence(timeout: timeout))
        XCTAssertTrue(app.buttons["chat-discard-pending"].waitForExistence(timeout: timeout))
    }
}
