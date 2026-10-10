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
        XCTAssertTrue(app.buttons["settings-button"].waitForExistence(timeout: timeout))
        app.buttons["settings-button"].tap()
        let scan = app.buttons["settings-scan-pairing"]
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
        XCTAssertTrue(confirm.isHittable)
        XCTAssertTrue(confirm.isEnabled)
        XCTAssertTrue(app.frame.contains(confirm.frame))
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
        for _ in 0..<12 {
            if session.exists {
                let visible = session.frame.intersection(fleet.frame)
                if !visible.isNull, visible.height >= min(160, session.frame.height), session.isHittable {
                    // Wrapped rows may be taller than the viewport. Tap their
                    // visible portion, without requiring the whole row to fit.
                    fleet.coordinate(withNormalizedOffset: .zero)
                        .withOffset(CGVector(dx: visible.midX - fleet.frame.minX,
                                             dy: visible.midY - fleet.frame.minY)).tap()
                    return
                }
            }
            let start = fleet.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.7))
            let end = fleet.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.45))
            start.press(forDuration: 0.05, thenDragTo: end, withVelocity: .slow, thenHoldForDuration: 0.3)
        }
        XCTFail("the wrapped sample session should be reachable at XXXL")
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
