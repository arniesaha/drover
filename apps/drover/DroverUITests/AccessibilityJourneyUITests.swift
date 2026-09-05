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

        checkDelivery.tap()
        let receiptCount = app.staticTexts["fixture-turn-receipt-count"]
        XCTAssertTrue(receiptCount.waitForExistence(timeout: timeout))
        XCTAssertEqual(receiptCount.label, "1", "checking delivery must not create a second turn")
        XCTAssertFalse(app.staticTexts["chat-delivery-manual-review"].exists)
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
    private func openFixtureChat(in app: XCUIApplication) {
        let session = app.buttons["fixture-session"]
        XCTAssertTrue(session.waitForExistence(timeout: timeout))

        // XXXL can place this particular fixture row just above the viewport.
        // Walk the real fleet list back toward its start, with a fixed bound,
        // then retain the reachability assertion below.
        let fleet = app.scrollViews.firstMatch
        XCTAssertTrue(fleet.exists, "the fleet list should remain scrollable at XXXL")
        for _ in 0..<2 where !session.isHittable {
            fleet.swipeDown()
        }
        XCTAssertTrue(session.isHittable, "the fleet session should remain reachable at XXXL")
        session.tap()

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
