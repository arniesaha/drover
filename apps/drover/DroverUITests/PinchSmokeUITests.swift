import XCTest

/// TEMPORARY pinch-zoom reproduction for issue triage — attaches to an
/// externally launched, env-override-authenticated app (README "Debug env
/// override"), opens the shell PTY session's terminal, and synthesizes a
/// pinch. Objective outcome is the persisted `terminalFontSize` default,
/// read from outside the test. Deleted after triage.
final class PinchSmokeUITests: XCTestCase {

    @MainActor
    func testPinchOnTerminal() throws {
        let app = XCUIApplication(bundleIdentifier: "com.arnab.drover")
        app.activate()

        let row = app.staticTexts["drover-smoke"].firstMatch
        XCTAssertTrue(row.waitForExistence(timeout: 15), "shell session row not found")
        row.tap()
        sleep(5)

        let shot1 = XCTAttachment(screenshot: app.screenshot())
        shot1.name = "20-terminal-before-pinch"
        shot1.lifetime = .keepAlways
        add(shot1)

        // Pinch out (zoom in) roughly at screen center.
        app.pinch(withScale: 2.0, velocity: 8.0)
        sleep(2)

        let shot2 = XCTAttachment(screenshot: app.screenshot())
        shot2.name = "21-terminal-after-pinch"
        shot2.lifetime = .keepAlways
        add(shot2)
    }
}
