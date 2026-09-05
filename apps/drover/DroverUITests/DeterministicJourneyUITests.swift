import XCTest

/// The credential-free core journey. It exercises the app's normal SwiftUI
/// navigation and the real `ChatModel`; the fixture only owns synthetic hub
/// transport below those boundaries.
final class DeterministicJourneyUITests: XCTestCase {
    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    private func makeCoreJourneyApp() -> XCUIApplication {
        let app = XCUIApplication()
        app.launchEnvironment["DROVER_UI_TEST_SCENARIO"] = "core-journey"
        // Each test gets an isolated fixture namespace. The identifier is not
        // a credential and persists across this test's terminate/relaunch.
        app.launchEnvironment["DROVER_UI_TEST_RUN_ID"] = UUID().uuidString
        return app
    }

    @MainActor
    func testFleetChatBackLaunchAndRecoveredDeliveryKeepsOneReceipt() throws {
        let app = makeCoreJourneyApp()
        app.launch()

        let session = app.buttons["fixture-session"]
        XCTAssertTrue(session.waitForExistence(timeout: 5))
        session.tap()

        let composer = app.textFields["composer-input"]
        XCTAssertTrue(composer.waitForExistence(timeout: 5))
        composer.tap()
        composer.typeText("fixture message")
        app.buttons["composer-send"].tap()
        XCTAssertTrue(
            app.staticTexts["chat-delivery-awaiting"].waitForExistence(timeout: 5),
            "the first synthetic receipt deliberately withholds its stream echo"
        )

        app.terminate()
        app.launch()

        let restoredSession = app.buttons["fixture-session"]
        XCTAssertTrue(restoredSession.waitForExistence(timeout: 5))
        restoredSession.tap()

        let checkDelivery = app.buttons["chat-check-delivery"]
        XCTAssertTrue(checkDelivery.waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["chat-copy-pending-to-draft"].exists)
        XCTAssertTrue(app.buttons["chat-discard-pending"].exists)
        XCTAssertEqual(app.buttons["chat-copy-pending-to-draft"].label, "Copy to draft")
        XCTAssertEqual(app.buttons["chat-discard-pending"].label, "Discard locally")
        XCTAssertTrue(app.staticTexts["chat-delivery-manual-review"].exists)
        XCTAssertEqual(app.staticTexts["fixture-turn-submission-count"].label, "1")
        checkDelivery.tap()

        XCTAssertTrue(app.staticTexts["chat-delivery-manual-review"].waitForNonExistence(timeout: 5))
        XCTAssertEqual(app.staticTexts["fixture-turn-receipt-count"].label, "1")
        XCTAssertEqual(app.staticTexts["fixture-turn-submission-count"].label, "1")

        // Confirm the normal fleet -> chat -> fleet navigation contract remains
        // intact after the recovery path clears the pending delivery.
        app.navigationBars.firstMatch.buttons.firstMatch.tap()
        XCTAssertTrue(app.buttons["fixture-session"].waitForExistence(timeout: 5))
        app.buttons["fixture-session"].tap()
        XCTAssertTrue(app.textFields["composer-input"].waitForExistence(timeout: 5))

        app.navigationBars.firstMatch.buttons.firstMatch.tap()
        let themeToggle = app.buttons["theme-toggle"]
        XCTAssertTrue(themeToggle.waitForExistence(timeout: 5))
        let previousThemeAction = themeToggle.label
        themeToggle.tap()
        XCTAssertNotEqual(app.buttons["theme-toggle"].label, previousThemeAction)

        app.buttons["launch-button"].tap()
        let launchComposer = app.textFields["composer-input"]
        XCTAssertTrue(launchComposer.waitForExistence(timeout: 5))
        launchComposer.tap()
        launchComposer.typeText("fixture launch")
        app.buttons["launch-confirm-button"].tap()
        XCTAssertTrue(app.staticTexts["Fixture ready: fixture-launched-session"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.textFields["composer-input"].exists)
    }

    @MainActor
    func testPendingDeliveryForAnotherSessionDoesNotAppearInTheCoreChat() throws {
        let app = makeCoreJourneyApp()
        app.launch()

        let otherSession = app.buttons["fixture-other-session"]
        XCTAssertTrue(otherSession.waitForExistence(timeout: 5))
        otherSession.tap()
        XCTAssertTrue(app.buttons["chat-check-delivery"].waitForExistence(timeout: 5))
        app.navigationBars.firstMatch.buttons.firstMatch.tap()

        let session = app.buttons["fixture-session"]
        XCTAssertTrue(session.waitForExistence(timeout: 5))
        session.tap()

        XCTAssertTrue(app.textFields["composer-input"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["chat-check-delivery"].exists)
        XCTAssertFalse(app.staticTexts["chat-delivery-manual-review"].exists)
        XCTAssertEqual(app.staticTexts["fixture-turn-submission-count"].label, "0")
    }

    @MainActor
    func testFirstComputerGuideKeepsQRAndManualPairingReachable() {
        let app = makeCoreJourneyApp()
        app.launchEnvironment["DROVER_UI_TEST_START_UNPAIRED"] = "1"
        app.launch()

        let setup = app.buttons["onboarding-setup-drover-button"]
        XCTAssertTrue(setup.waitForExistence(timeout: 5))
        setup.tap()
        XCTAssertTrue(app.buttons["server-setup-mode-hub"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["server-setup-mode-host"].exists)
        XCTAssertTrue(app.staticTexts["server-setup-command-text"].label.contains("install.sh"))
        let next = app.buttons["onboarding-next-pair-button"]
        if !next.isHittable { app.swipeUp() }
        next.tap()
        XCTAssertTrue(app.otherElements["onboarding-qr-scanner"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.textFields["onboarding-server-url-field"].exists)
        XCTAssertTrue(app.textFields["onboarding-pairing-code-field"].exists)
        XCTAssertTrue(app.buttons["onboarding-pair-submit-button"].exists)
    }

    @MainActor
    func testConnectedFleetGuideShowsOnlyAdditionalHostSetup() {
        let app = makeCoreJourneyApp()
        app.launch()
        XCTAssertTrue(app.buttons["fixture-session"].waitForExistence(timeout: 5))
        app.buttons["settings-button"].tap()
        let guide = app.buttons["settings-server-setup-guide"]
        XCTAssertTrue(guide.waitForExistence(timeout: 5))
        guide.tap()
        XCTAssertTrue(app.buttons["server-setup-mode-host"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["server-setup-mode-hub"].exists)
        XCTAssertTrue(app.staticTexts["server-setup-command-text"].label.contains("pair-host"))
    }
}
