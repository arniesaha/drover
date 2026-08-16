import XCTest

/// Drives the real first-launch onboarding flow against the LIVE server with
/// a deliberately invalid pairing code, asserting the inline rejection
/// message renders.
/// Run explicitly via the `DroverUITests` scheme — not part of the unit-test
/// scheme. The trailing hold keeps the rejection on screen long enough for
/// an external `simctl io booted screenshot` to capture it.
///
/// The server URL comes from `DROVER_SMOKE_URL` in the *runner's* environment
/// (falls back to loopback); no real credential enters the app.
final class SettingsSmokeUITests: XCTestCase {

    override func setUpWithError() throws {
        continueAfterFailure = false
        addUIInterruptionMonitor(withDescription: "System dialogs") { alert in
            MainActor.assumeIsolated {
                for label in ["Allow", "Not Now", "OK"] {
                    let button = alert.buttons[label]
                    if button.exists {
                        button.tap()
                        return true
                    }
                }
                return false
            }
        }
    }

    @MainActor
    func testInvalidPairingCodeShowsRejectionInline() throws {
        let app = XCUIApplication()
        app.launchEnvironment["DROVER_BASE_URL"] = " "
        app.launchEnvironment["DROVER_TOKEN"] = " "
        app.launchArguments += ["-drover.server.url", " "]
        app.launch()

        let alreadyHaveServer = app.buttons["onboarding-already-have-server-button"]
        XCTAssertTrue(alreadyHaveServer.waitForExistence(timeout: 10),
                      "first launch should show the onboarding welcome screen")
        alreadyHaveServer.tap()

        let urlField = app.textFields["onboarding-server-url-field"]
        XCTAssertTrue(urlField.waitForExistence(timeout: 10),
                      "manual pairing should show the server URL field")
        urlField.tap()
        let serverURL = ProcessInfo.processInfo.environment["DROVER_SMOKE_URL"]
            ?? "http://127.0.0.1:7080"
        urlField.typeText(serverURL)

        let codeField = app.textFields["onboarding-pairing-code-field"]
        XCTAssertTrue(codeField.waitForExistence(timeout: 5))
        codeField.tap()
        codeField.typeText("XXXX-XXXX")

        let pairButton = app.buttons["onboarding-pair-submit-button"]
        XCTAssertTrue(pairButton.isEnabled)
        pairButton.tap()

        let rejection = app.staticTexts["onboarding-pair-error"]
        XCTAssertTrue(rejection.waitForExistence(timeout: 15),
                      "invalid pairing code should surface the inline rejection")
        XCTAssertEqual(rejection.label, "unknown or expired code")

        // Hold so an external screenshot can capture the state.
        Thread.sleep(forTimeInterval: 8)
    }

    @MainActor
    func testWorkerHostShowsPrimaryHubPairingCommand() {
        let app = XCUIApplication()
        app.launchEnvironment["DROVER_BASE_URL"] = " "
        app.launchEnvironment["DROVER_TOKEN"] = " "
        app.launchArguments += ["-drover.server.url", " "]
        app.launch()

        let setUpDrover = app.buttons["onboarding-setup-drover-button"]
        XCTAssertTrue(setUpDrover.waitForExistence(timeout: 10))
        setUpDrover.tap()

        let workerHost = app.buttons["server-setup-mode-host"]
        XCTAssertTrue(workerHost.waitForExistence(timeout: 10))
        workerHost.tap()

        let command = app.staticTexts["server-setup-command-text"]
        XCTAssertTrue(command.waitForExistence(timeout: 5))
        XCTAssertEqual(command.label, "drover-server pair-host --name <host-name>")
        XCTAssertTrue(app.staticTexts["Pairing Command"].exists)
        XCTAssertTrue(app.staticTexts[
            "Run this command on the primary hub. Then run the installer command it prints on the additional machine."
        ].exists)
    }
}
