import XCTest

/// Credential-free terminal coverage through the app root. The DEBUG fixture
/// owns the PTY stream and emits a fixed prompt; this test never sends input
/// or creates a real session.
final class TerminalFixtureUITests: XCTestCase {
    @MainActor
    func testCapabilityFixtureRendersTerminalAndReturnsToFleet() {
        let app = XCUIApplication()
        app.launchEnvironment["DROVER_UI_TEST_SCENARIO"] = "capability-journey"
        app.launchEnvironment["DROVER_UI_TEST_RUN_ID"] = UUID().uuidString
        app.launch()

        let fleet = app.scrollViews["fleet-list"]
        XCTAssertTrue(fleet.waitForExistence(timeout: 8))
        let terminalRow = fleet.buttons.matching(
            NSPredicate(format: "label CONTAINS %@", "Fixture terminal journey")
        ).firstMatch
        for _ in 0..<4 where !terminalRow.exists {
            fleet.swipeUp()
        }
        XCTAssertTrue(terminalRow.waitForExistence(timeout: 8))
        terminalRow.tap()

        let terminalMenu = app.buttons["terminal-menu"]
        XCTAssertTrue(terminalMenu.waitForExistence(timeout: 8))
        XCTAssertTrue(app.descendants(matching: .any)["terminal-harness-title"].exists)
        // A fresh disposable simulator can show the system keyboard's own
        // onboarding tip when Terminal focuses its input. It is not part of
        // the product journey, so dismiss it before recording the released
        // Terminal surface. The conditional keeps normal simulator runs
        // unchanged after the one-time OS tip has already been acknowledged.
        let keyboardTip = app.buttons["Continue"]
        if keyboardTip.exists {
            keyboardTip.tap()
        }
        let terminal = XCTAttachment(screenshot: app.screenshot())
        terminal.name = "Fixture terminal synthetic prompt"
        terminal.lifetime = .keepAlways
        add(terminal)

        app.navigationBars.buttons.firstMatch.tap()
        XCTAssertTrue(terminalRow.waitForExistence(timeout: 8))
        XCTAssertFalse(terminalMenu.exists)
    }
}
