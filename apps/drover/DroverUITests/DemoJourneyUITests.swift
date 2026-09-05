import XCTest

final class DemoJourneyUITests: XCTestCase {
    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    @MainActor
    private func makeFirstRunApp() -> XCUIApplication {
        let app = XCUIApplication()
        app.launchEnvironment["DROVER_UI_TEST_RESET_AUTH"] = "1"
        return app
    }

    @MainActor
    private func makeReturningFixtureApp() -> XCUIApplication {
        let app = XCUIApplication()
        app.launchEnvironment["DROVER_UI_TEST_SCENARIO"] = "core-journey"
        app.launchEnvironment["DROVER_UI_TEST_RUN_ID"] = UUID().uuidString
        return app
    }

    @MainActor
    func testFirstRunDemoShowsFleetApprovalReconnectLaunchAndExit() throws {
        let app = makeFirstRunApp()
        app.launch()

        let tryDemo = app.buttons["onboarding-try-demo"]
        XCTAssertTrue(tryDemo.waitForExistence(timeout: 5))
        if !tryDemo.isHittable { app.swipeUp() }
        tryDemo.tap()
        XCTAssertTrue(app.staticTexts["demo-mode-label"].waitForExistence(timeout: 5))

        let approvalSession = app.buttons["demo-approval-session"]
        XCTAssertTrue(approvalSession.waitForExistence(timeout: 5))
        approvalSession.tap()
        let allow = app.buttons["approval-allow"]
        XCTAssertTrue(allow.waitForExistence(timeout: 5))
        allow.tap()
        XCTAssertTrue(allow.waitForNonExistence(timeout: 5))

        app.buttons["demo-reconnect"].tap()
        XCTAssertTrue(app.otherElements["chat-reconnecting"].waitForExistence(timeout: 5))

        app.navigationBars.firstMatch.buttons.firstMatch.tap()
        app.buttons["demo-reset"].tap()
        XCTAssertTrue(app.buttons["demo-approval-session"].waitForExistence(timeout: 5))
        let launch = app.buttons["launch-button"]
        XCTAssertTrue(launch.waitForExistence(timeout: 5))
        launch.tap()
        let launchConfirm = app.buttons["launch-confirm-button"]
        XCTAssertTrue(launchConfirm.waitForExistence(timeout: 5))
        launchConfirm.tap()
        XCTAssertTrue(app.textFields["composer-input"].waitForExistence(timeout: 5))

        app.navigationBars.firstMatch.buttons.firstMatch.tap()
        let exit = app.buttons["demo-exit"]
        XCTAssertTrue(exit.waitForExistence(timeout: 5))
        exit.tap()
        XCTAssertTrue(app.staticTexts["onboarding-welcome-headline"].waitForExistence(timeout: 5))
    }

    @MainActor
    func testReturningUserDemoPreservesTheExistingFixtureDraft() throws {
        let app = makeReturningFixtureApp()
        app.launch()

        let session = app.buttons["fixture-session"]
        XCTAssertTrue(session.waitForExistence(timeout: 5))
        session.tap()
        let composer = app.textFields["composer-input"]
        XCTAssertTrue(composer.waitForExistence(timeout: 5))
        composer.tap()
        composer.typeText("fixture draft before demo")

        app.navigationBars.firstMatch.buttons.firstMatch.tap()
        let settings = app.buttons["settings-button"]
        XCTAssertTrue(settings.waitForExistence(timeout: 5))
        settings.tap()
        let tryDemo = app.buttons["settings-try-demo"]
        XCTAssertTrue(tryDemo.waitForExistence(timeout: 5))
        if !tryDemo.isHittable { app.swipeUp() }
        tryDemo.tap()
        XCTAssertTrue(app.staticTexts["demo-mode-label"].waitForExistence(timeout: 5))
        app.buttons["demo-exit"].tap()

        let restoredSession = app.buttons["fixture-session"]
        XCTAssertTrue(restoredSession.waitForExistence(timeout: 5))
        restoredSession.tap()
        let restoredComposer = app.textFields["composer-input"]
        XCTAssertTrue(restoredComposer.waitForExistence(timeout: 5))
        XCTAssertEqual(restoredComposer.value as? String, "fixture draft before demo")
    }
}
