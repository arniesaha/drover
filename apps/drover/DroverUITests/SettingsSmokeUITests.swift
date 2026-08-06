import XCTest

/// Drives the real Settings onboarding flow against the LIVE server with a
/// deliberately wrong token, asserting the inline rejection message renders.
/// Run explicitly via the `DroverUITests` scheme — not part of the unit-test
/// scheme. The trailing hold keeps the rejection on screen long enough for
/// an external `simctl io booted screenshot` to capture it.
///
/// The server URL comes from `DROVER_SMOKE_URL` in the *runner's* environment
/// (falls back to the LAN default); the token typed is always the literal
/// `wrong-token` — never a real credential.
final class SettingsSmokeUITests: XCTestCase {
    @MainActor
    func testWrongTokenShowsRejectionInline() throws {
        let app = XCUIApplication()
        // No DROVER_BASE_URL/DROVER_TOKEN in launchEnvironment → the debug
        // override is inert and the app starts on the onboarding SettingsView.
        app.launch()

        let urlField = app.textFields["http://host:7080"]
        XCTAssertTrue(urlField.waitForExistence(timeout: 10), "onboarding URL field should show")
        urlField.tap()
        let serverURL = ProcessInfo.processInfo.environment["DROVER_SMOKE_URL"]
            ?? "http://127.0.0.1:7080"
        urlField.typeText(serverURL)

        let tokenField = app.secureTextFields["API token"]
        XCTAssertTrue(tokenField.waitForExistence(timeout: 5))
        tokenField.tap()
        tokenField.typeText("wrong-token")

        let saveButton = app.buttons["Test & Save"]
        XCTAssertTrue(saveButton.isEnabled)
        saveButton.tap()

        let rejection = app.staticTexts["Token rejected by server."]
        XCTAssertTrue(rejection.waitForExistence(timeout: 15),
                      "wrong token should surface the inline rejection")

        // Hold so an external screenshot can capture the state.
        Thread.sleep(forTimeInterval: 8)
    }
}
