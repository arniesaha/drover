import XCTest

/// Live end-to-end validation against a real drover-server: onboarding login,
/// session launch, chat round-trips, navigation resume, and terminate.
/// Run explicitly via the `DroverUITests` scheme with the runner env
/// providing `DROVER_SMOKE_URL` and `DROVER_SMOKE_TOKEN`
/// (`TEST_RUNNER_DROVER_SMOKE_*` on the xcodebuild invocation). Skipped when
/// no token is provided so the default UI-test scheme stays self-contained.
final class E2EValidationUITests: XCTestCase {

    override func setUpWithError() throws {
        continueAfterFailure = false
        // The notification-permission alert (and any save-password prompt)
        // is springboard-owned and can appear at an unpredictable moment
        // during onboarding. An interruption monitor is the reliable way to
        // clear it: XCUITest fires it on the next interaction that finds the
        // app blocked, dismisses the alert, and retries the interaction.
        addUIInterruptionMonitor(withDescription: "System dialogs") { alert in
            for label in ["Allow", "Not Now", "Don’t Save", "OK"] {
                let button = alert.buttons[label]
                if button.exists {
                    button.tap()
                    return true
                }
            }
            return false
        }
    }

    @MainActor
    func testLoginLaunchChatResumeTerminate() throws {
        let env = ProcessInfo.processInfo.environment
        guard let token = env["DROVER_SMOKE_TOKEN"], !token.isEmpty else {
            throw XCTSkip("DROVER_SMOKE_TOKEN not set — live E2E skipped")
        }
        let serverURL = env["DROVER_SMOKE_URL"] ?? "http://127.0.0.1:7080"
        let cwd = env["DROVER_SMOKE_CWD"] ?? "/private/tmp/drover-e2e"

        let app = XCUIApplication()
        app.launch()

        // ── 1. Onboarding / login ─────────────────────────────────────────
        let urlField = app.textFields["http://host:7080"]
        XCTAssertTrue(urlField.waitForExistence(timeout: 10), "onboarding URL field should show")
        shoot(app, "01-onboarding")
        urlField.tap()
        urlField.typeText(serverURL)

        let tokenField = app.secureTextFields["API token"]
        XCTAssertTrue(tokenField.waitForExistence(timeout: 5))
        tokenField.tap()
        tokenField.typeText(token)

        let saveButton = app.buttons["Test & Save"]
        XCTAssertTrue(saveButton.isEnabled, "Test & Save should enable once both fields are filled")
        saveButton.tap()

        // Successful configure flips the root to the Sessions list and asks
        // for notification permission. The system alert belongs to
        // springboard and can land at any point in the transition, so poll
        // for the Sessions toolbar while dismissing the alert whenever it
        // shows, instead of two fixed sequential waits.
        let launchButton = app.buttons["launch-button"]
        XCTAssertTrue(launchButton.waitForExistence(timeout: 45), "login should land on Sessions")
        shoot(app, "02-sessions-after-login")

        // ── 2. Launch a structured session ────────────────────────────────
        // The notification-permission alert can appear a beat AFTER the list
        // loads and land over the toolbar. Tapping launch triggers the
        // interruption monitor (which dismisses the alert); retry until the
        // New Session sheet actually presents. `app.tap()` after each attempt
        // nudges the monitor to fire even if the first tap was swallowed.
        let launchBar = app.navigationBars["New Session"]
        let openDeadline = Date().addingTimeInterval(30)
        while Date() < openDeadline && !launchBar.exists {
            launchButton.tap()
            if launchBar.waitForExistence(timeout: 3) { break }
            // Interruption monitors only fire on an interaction; if a system
            // alert swallowed the tap, tapping a neutral element (the nav-bar
            // title — never a session row) fires the monitor so the next
            // loop's launch tap lands.
            app.navigationBars["Sessions"].tap()
        }
        XCTAssertTrue(launchBar.exists, "tapping + should present the New Session sheet")

        // Prefer the Mac Mini host when the picker offers it (deterministic
        // target); otherwise keep the model's default.
        let hostPicker = app.buttons["Host"]
        if hostPicker.waitForExistence(timeout: 3) {
            hostPicker.tap()
            let mac = app.buttons["Mac Mini"]
            if mac.waitForExistence(timeout: 3) {
                mac.tap()
            } else {
                // Close the menu without changing the selection.
                app.tap()
            }
        }

        let cwdField = app.textFields["cwd (optional)"]
        XCTAssertTrue(cwdField.waitForExistence(timeout: 5))
        cwdField.tap()
        cwdField.typeText(cwd)

        let promptEditor = app.textViews.firstMatch
        XCTAssertTrue(promptEditor.waitForExistence(timeout: 5),
                      "structured harness should show the starting-prompt editor")
        promptEditor.tap()
        promptEditor.typeText("Reply with exactly the single word: HORSERADISH")
        shoot(app, "03-launch-sheet")

        app.buttons["launch-confirm-button"].tap()

        // ── 3. Chat: wait for the model's reply ───────────────────────────
        let chatBar = app.navigationBars["Chat"]
        XCTAssertTrue(chatBar.waitForExistence(timeout: 30), "launch should push straight into Chat")
        shoot(app, "04-chat-connected")

        let horseradish = app.staticTexts["HORSERADISH"]
        XCTAssertTrue(horseradish.waitForExistence(timeout: 240),
                      "the starting prompt should produce the exact reply bubble")
        shoot(app, "05-chat-first-reply")

        // ── 4. A follow-up turn through the composer ──────────────────────
        // TextField(axis: .vertical) may surface as either element type.
        var composer = app.textFields["Message"]
        if !composer.waitForExistence(timeout: 5) {
            composer = app.textViews["Message"]
            XCTAssertTrue(composer.waitForExistence(timeout: 5))
        }
        composer.tap()
        composer.typeText("How many legs does a spider have? Reply with only the number.")
        let send = app.buttons["composer-send"]
        XCTAssertTrue(send.isEnabled)
        send.tap()

        let eight = app.staticTexts["8"]
        XCTAssertTrue(eight.waitForExistence(timeout: 240),
                      "follow-up turn should round-trip through the composer")
        shoot(app, "06-chat-second-reply")

        // ── 5. Navigate back, find the session in the list, resume it ─────
        app.navigationBars["Chat"].buttons.firstMatch.tap()
        XCTAssertTrue(launchButton.waitForExistence(timeout: 10))
        shoot(app, "07-sessions-with-live-session")

        // Reopen the freshly-launched session. Its cwd's last component is the
        // row title; `.firstMatch` guards against any older same-cwd rows
        // lingering in the list from prior runs.
        let row = app.staticTexts[URL(fileURLWithPath: cwd).lastPathComponent].firstMatch
        XCTAssertTrue(row.waitForExistence(timeout: 30),
                      "the launched session should appear in a bucket")
        row.tap()

        XCTAssertTrue(chatBar.waitForExistence(timeout: 10), "row should reopen the chat")
        XCTAssertTrue(horseradish.waitForExistence(timeout: 60),
                      "reopening the session should replay the transcript")
        shoot(app, "08-chat-resumed")

        // ── 6. Continue the session via the toolbar menu ──────────────────
        // Continuing a structured-capable session creates a fresh structured
        // session whose first turn is the server-built handoff context — it
        // opens another Chat screen, not a terminal.
        app.buttons["chat-menu"].tap()
        let handOff = app.buttons["Continue in a new session"]
        XCTAssertTrue(handOff.waitForExistence(timeout: 5))
        shoot(app, "09-chat-menu")
        handOff.tap()

        let handoffContext = app.staticTexts.matching(
            NSPredicate(format: "label CONTAINS 'Continue this Drover Harness session'")
        ).firstMatch
        XCTAssertTrue(handoffContext.waitForExistence(timeout: 60),
                      "handoff should open a structured chat seeded with the handoff context")
        XCTAssertFalse(app.navigationBars["Terminal"].exists,
                       "structured handoff must not land in the Terminal screen")
        shoot(app, "10-handoff-chat")

        // ── 7. Back out; original session is still reachable ──────────────
        app.navigationBars["Chat"].buttons.firstMatch.tap()
        XCTAssertTrue(chatBar.waitForExistence(timeout: 10),
                      "backing out of the handoff chat returns to the source chat")
        shoot(app, "11-back-on-source-chat")

        // Hold briefly so external screenshots can catch the final state.
        Thread.sleep(forTimeInterval: 3)
    }

    // MARK: - Helpers

    @MainActor
    private func shoot(_ app: XCUIApplication, _ name: String) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
