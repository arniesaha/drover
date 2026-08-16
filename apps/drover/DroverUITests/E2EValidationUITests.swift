import Foundation
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
            // The handler is delivered on the main thread but typed as a
            // nonisolated @Sendable closure, and XCUIElement is main-actor
            // isolated — so under Swift 6 checking this needs an explicit
            // assumption rather than a hop, since the monitor has to answer
            // synchronously with whether it handled the alert.
            MainActor.assumeIsolated {
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
    }

    @MainActor
    func testDeterministicChatHeaderLayoutAtAccessibilitySize() {
        let expectedTitle = "Improving live session recaps across every narrow chat header"
        let expectedMetadata = "Codex · ctx 93.6K / 258.4K · 36%"
        let app = XCUIApplication()
        app.launchEnvironment["DROVER_UI_TEST_CHAT_HEADER_FIXTURE"] = "1"
        app.launchEnvironment["DROVER_UI_TEST_CHAT_HEADER_TITLE"] = expectedTitle
        app.launchEnvironment["DROVER_UI_TEST_CHAT_HEADER_METADATA"] = expectedMetadata
        app.launch()

        let title = app.staticTexts["chat-recap-title"]
        let metadata = app.staticTexts["chat-header-metadata"]
        let menu = app.buttons["chat-menu"]
        let navigationBar = app.navigationBars.firstMatch
        XCTAssertTrue(title.waitForExistence(timeout: 10))
        XCTAssertTrue(metadata.waitForExistence(timeout: 10))
        XCTAssertTrue(menu.exists)
        XCTAssertTrue(navigationBar.exists)
        XCTAssertEqual(title.label, expectedTitle)
        XCTAssertEqual(metadata.label, expectedMetadata)

        // The full accessibility labels remain available while both visible
        // frames stay on one line, ordered, and inside the compact nav bar.
        XCTAssertLessThanOrEqual(title.frame.height, 40)
        XCTAssertLessThanOrEqual(metadata.frame.height, 30)
        XCTAssertLessThanOrEqual(title.frame.maxY, metadata.frame.minY + 1)
        XCTAssertGreaterThanOrEqual(title.frame.minX, navigationBar.frame.minX)
        XCTAssertLessThanOrEqual(title.frame.maxX, navigationBar.frame.maxX)
        XCTAssertLessThanOrEqual(title.frame.maxX, menu.frame.minX)
        XCTAssertLessThanOrEqual(metadata.frame.maxY, navigationBar.frame.maxY + 1)
    }

    @MainActor
    func testLoginLaunchChatResumeTerminate() async throws {
        let env = ProcessInfo.processInfo.environment
        guard let token = env["DROVER_SMOKE_TOKEN"], !token.isEmpty else {
            throw XCTSkip("DROVER_SMOKE_TOKEN not set — live E2E skipped")
        }
        let serverURL = env["DROVER_SMOKE_URL"] ?? "http://127.0.0.1:7080"
        let cwd = env["DROVER_SMOKE_CWD"] ?? "/private/tmp"
        let deviceLabel = "Drover UI E2E \(UUID().uuidString)"
        let preRunCredentials = try await listCredentials(serverURL: serverURL, token: token)

        let app = XCUIApplication()
        addTeardownBlock { @MainActor in
            await self.cleanUpCredentialedRun(
                app: app,
                serverURL: serverURL,
                token: token,
                deviceLabel: deviceLabel,
                preRunCredentialIDs: Set(preRunCredentials.map(\.id))
            )
        }

        let pairingCode = try await mintDevicePairingCode(
            serverURL: serverURL,
            token: token,
            label: deviceLabel
        )

        app.launchEnvironment["DROVER_BASE_URL"] = " "
        app.launchEnvironment["DROVER_TOKEN"] = " "
        app.launchEnvironment["DROVER_UI_TEST_DEVICE_NAME"] = deviceLabel
        app.launchArguments += ["-drover.server.url", " "]
        app.launch()

        // ── 1. Onboarding / login ─────────────────────────────────────────
        let alreadyHaveServer = app.buttons["onboarding-already-have-server-button"]
        XCTAssertTrue(alreadyHaveServer.waitForExistence(timeout: 10),
                      "first launch should show the onboarding welcome screen")
        alreadyHaveServer.tap()

        let urlField = app.textFields["onboarding-server-url-field"]
        XCTAssertTrue(urlField.waitForExistence(timeout: 10),
                      "manual pairing should show the server URL field")
        shoot(app, "01-onboarding")
        urlField.tap()
        urlField.typeText(serverURL)

        let codeField = app.textFields["onboarding-pairing-code-field"]
        XCTAssertTrue(codeField.waitForExistence(timeout: 5))
        codeField.tap()
        codeField.typeText(pairingCode)

        let pairButton = app.buttons["onboarding-pair-submit-button"]
        XCTAssertTrue(pairButton.isEnabled,
                      "Pair should enable once the server and code are filled")
        pairButton.tap()

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
            // alert swallowed the tap, tapping a neutral element (the
            // wordmark in the header row — never a session row, and never the
            // theme toggle beside it) fires the monitor so the next loop's
            // launch tap lands.
            app.staticTexts["drover-wordmark"].tap()
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

        let promptEditor = app.textFields["prompt-input"]
        XCTAssertTrue(promptEditor.waitForExistence(timeout: 5),
                      "structured harness should show the starting-prompt editor")
        promptEditor.tap()
        promptEditor.typeText("Reply with exactly the single word: HORSERADISH")
        let dismissKeyboard = app.buttons["keyboard-dismiss"]
        XCTAssertTrue(dismissKeyboard.waitForExistence(timeout: 5))
        dismissKeyboard.tap()
        shoot(app, "03-launch-sheet")

        app.buttons["launch-confirm-button"].tap()

        // ── 3. Chat: wait for the model's reply ───────────────────────────
        let chatMenu = app.buttons["chat-menu"]
        XCTAssertTrue(chatMenu.waitForExistence(timeout: 30),
                      "launch should push straight into Chat")
        shoot(app, "04-chat-connected")

        let horseradish = app.staticTexts["HORSERADISH"]
        XCTAssertTrue(horseradish.waitForExistence(timeout: 240),
                      "the starting prompt should produce the exact reply bubble")

        shoot(app, "05-chat-first-reply")

        // ── 4. A follow-up turn through the composer ──────────────────────
        let composer = app.textFields["prompt-input"]
        XCTAssertTrue(composer.waitForExistence(timeout: 5))
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
        app.navigationBars.firstMatch.buttons.firstMatch.tap()
        XCTAssertTrue(launchButton.waitForExistence(timeout: 10))
        shoot(app, "07-sessions-with-live-session")

        // Reopen the freshly-launched session. Its cwd's last component is the
        // row title; `.firstMatch` guards against any older same-cwd rows
        // lingering in the list from prior runs.
        let row = app.staticTexts[URL(fileURLWithPath: cwd).lastPathComponent].firstMatch
        XCTAssertTrue(row.waitForExistence(timeout: 30),
                      "the launched session should appear in a bucket")
        row.tap()

        XCTAssertTrue(chatMenu.waitForExistence(timeout: 10), "row should reopen the chat")
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
        app.navigationBars.firstMatch.buttons.firstMatch.tap()
        XCTAssertTrue(chatMenu.waitForExistence(timeout: 10),
                      "backing out of the handoff chat returns to the source chat")
        shoot(app, "11-back-on-source-chat")

        // Hold briefly so external screenshots can catch the final state.
        try await Task.sleep(for: .seconds(3))
    }

    /// Header-specific fixture: `DROVER_SMOKE_RECAP_SESSION_ID` must name a
    /// Codex structured session whose snapshot row already carries the recap
    /// below and whose event stream includes a Codex turn-completion payload
    /// with context usage. Keeping that fixture separate from the mutating
    /// launch/resume smoke test makes the recap and provider deterministic.
    @MainActor
    func testSeededCodexRecapHeader() throws {
        let env = ProcessInfo.processInfo.environment
        guard let token = env["DROVER_SMOKE_TOKEN"], !token.isEmpty else {
            throw XCTSkip("DROVER_SMOKE_TOKEN not set — seeded recap E2E skipped")
        }
        guard let sessionID = env["DROVER_SMOKE_RECAP_SESSION_ID"], !sessionID.isEmpty else {
            XCTFail(
                "DROVER_SMOKE_RECAP_SESSION_ID is required when live E2E is enabled; "
                    + "seed a Codex recap session before running this suite"
            )
            return
        }
        let serverURL = env["DROVER_SMOKE_URL"] ?? "http://127.0.0.1:7080"
        let expectedRecap = "Improving previews; verifying the chat header."

        let app = XCUIApplication()
        app.launchEnvironment["DROVER_BASE_URL"] = serverURL
        app.launchEnvironment["DROVER_TOKEN"] = token
        app.launch()

        let row = app.buttons[sessionID]
        XCTAssertTrue(row.waitForExistence(timeout: 30),
                      "seeded Codex recap session should be listed")
        row.tap()

        let title = app.staticTexts.matching(NSPredicate(
            format: "identifier == %@ AND label == %@",
            "chat-recap-title", expectedRecap
        )).firstMatch
        XCTAssertTrue(title.waitForExistence(timeout: 30),
                      "chat should render the seeded recap title")
        XCTAssertEqual(title.label, expectedRecap)

        let metadata = app.staticTexts.matching(NSPredicate(
            format: "identifier == %@ AND label CONTAINS[c] %@ AND label CONTAINS[c] %@",
            "chat-header-metadata", "Codex", "ctx"
        )).firstMatch
        XCTAssertTrue(metadata.waitForExistence(timeout: 30),
                      "chat should render Codex context metadata")
        XCTAssertTrue(metadata.label.contains("Codex"))
        XCTAssertTrue(metadata.label.contains("ctx"))
    }

    /// Diagnostic reproduction against an explicitly selected long session.
    /// This does not create or terminate a session; it records transcript
    /// visibility before/after the composer collapses on send, then after a
    /// back-and-reopen navigation cycle.
    @MainActor
    func testExistingLongChatDoesNotBlankAcrossSendAndReopen() throws {
        let env = ProcessInfo.processInfo.environment
        guard let token = env["DROVER_SMOKE_TOKEN"], !token.isEmpty else {
            throw XCTSkip("DROVER_SMOKE_TOKEN not set — live diagnostic skipped")
        }
        let serverURL = env["DROVER_SMOKE_URL"] ?? "http://127.0.0.1:7080"
        guard let sessionID = env["DROVER_SMOKE_SESSION_ID"], !sessionID.isEmpty else {
            throw XCTSkip("DROVER_SMOKE_SESSION_ID not set — live diagnostic skipped")
        }

        let app = XCUIApplication()
        app.launchEnvironment["DROVER_BASE_URL"] = serverURL
        app.launchEnvironment["DROVER_TOKEN"] = token
        app.launch()

        let row = app.buttons[sessionID]
        XCTAssertTrue(row.waitForExistence(timeout: 30), "target session should be listed")
        row.tap()
        XCTAssertTrue(app.buttons["composer-send"].waitForExistence(timeout: 30))
        XCTAssertTrue(app.buttons["session-events-row"].firstMatch.waitForExistence(timeout: 30),
                      "the structured transcript should render a folded status row")
        shoot(app, "reset-01-loaded")

        var composer = app.textFields["Add feedback..."]
        if !composer.waitForExistence(timeout: 3) {
            composer = app.textViews["Add feedback..."]
            XCTAssertTrue(composer.waitForExistence(timeout: 3))
        }
        composer.tap()
        composer.typeText("Viewport stability diagnostic. Reply with exactly: STABLE")
        app.buttons["composer-send"].tap()

        for (index, delay) in [0.05, 0.15, 0.4, 1.0].enumerated() {
            Thread.sleep(forTimeInterval: delay)
            shoot(app, "reset-02-after-send-\(index)")
        }

        app.navigationBars.buttons.firstMatch.tap()
        XCTAssertTrue(row.waitForExistence(timeout: 15))
        row.tap()
        XCTAssertTrue(app.buttons["composer-send"].waitForExistence(timeout: 15))
        XCTAssertTrue(app.buttons["session-events-row"].firstMatch.waitForExistence(timeout: 30),
                      "reopening should render the structured transcript")
        shoot(app, "reset-03-reopened")
    }

    // MARK: - Helpers

    private struct PairingCodeResponse: Decodable {
        let code: String
    }

    private struct CredentialListResponse: Decodable {
        let credentials: [CredentialSummary]
    }

    private struct CredentialSummary: Decodable {
        let id: String
        let scope: String
        let label: String
        let revokedAt: String?

        enum CodingKeys: String, CodingKey {
            case id
            case scope
            case label
            case revokedAt = "revoked_at"
        }
    }

    @MainActor
    private func mintDevicePairingCode(
        serverURL: String,
        token: String,
        label: String
    ) async throws -> String {
        let baseURL = try XCTUnwrap(URL(string: serverURL), "DROVER_SMOKE_URL must be a URL")
        var request = URLRequest(url: baseURL.appendingPathComponent("auth/pair-codes"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.httpBody = try JSONSerialization.data(
            withJSONObject: ["scope": "device", "label": label]
        )

        let (data, response) = try await URLSession.shared.data(for: request)
        let http = try XCTUnwrap(response as? HTTPURLResponse)
        XCTAssertEqual(http.statusCode, 201, "hub should mint a device pairing code")
        return try JSONDecoder().decode(PairingCodeResponse.self, from: data).code
    }

    @MainActor
    private func cleanUpCredentialedRun(
        app: XCUIApplication,
        serverURL: String,
        token: String,
        deviceLabel: String,
        preRunCredentialIDs: Set<String>
    ) async {
        do {
            let afterRun = try await listCredentials(serverURL: serverURL, token: token)
            let attributable = afterRun.filter {
                !preRunCredentialIDs.contains($0.id)
                    && $0.scope == "device"
                    && $0.label == deviceLabel
                    && $0.revokedAt == nil
            }
            for credential in attributable {
                try await revokeCredential(
                    credential.id,
                    serverURL: serverURL,
                    token: token
                )
            }

            let afterCleanup = try await listCredentials(serverURL: serverURL, token: token)
            let remaining = afterCleanup.filter {
                !preRunCredentialIDs.contains($0.id)
                    && $0.scope == "device"
                    && $0.label == deviceLabel
                    && $0.revokedAt == nil
            }
            XCTAssertTrue(
                remaining.isEmpty,
                "the E2E run must not leave its device credential active"
            )
            print(
                "E2E credential cleanup: pre_run=\(preRunCredentialIDs.count) "
                    + "attributable=\(attributable.count) active_after=\(remaining.count)"
            )
        } catch {
            XCTFail("E2E server credential cleanup failed: \(error.localizedDescription)")
        }

        app.terminate()
        app.launchEnvironment["DROVER_BASE_URL"] = " "
        app.launchEnvironment["DROVER_TOKEN"] = " "
        app.launchEnvironment["DROVER_UI_TEST_RESET_AUTH"] = "1"
        app.launchArguments = ["-drover.server.url", " "]
        app.launch()
        XCTAssertTrue(
            app.buttons["onboarding-already-have-server-button"].waitForExistence(timeout: 10),
            "teardown relaunch must clear the app credential and return to onboarding"
        )
        app.terminate()
    }

    @MainActor
    private func listCredentials(
        serverURL: String,
        token: String
    ) async throws -> [CredentialSummary] {
        let baseURL = try XCTUnwrap(URL(string: serverURL), "DROVER_SMOKE_URL must be a URL")
        var request = URLRequest(url: baseURL.appendingPathComponent("auth/credentials"))
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")

        let (data, response) = try await URLSession.shared.data(for: request)
        let http = try XCTUnwrap(response as? HTTPURLResponse)
        guard http.statusCode == 200 else {
            throw NSError(
                domain: "DroverE2ECredentialCleanup",
                code: http.statusCode,
                userInfo: [NSLocalizedDescriptionKey: "credential list returned HTTP \(http.statusCode)"]
            )
        }
        return try JSONDecoder().decode(CredentialListResponse.self, from: data).credentials
    }

    @MainActor
    private func revokeCredential(
        _ credentialID: String,
        serverURL: String,
        token: String
    ) async throws {
        let baseURL = try XCTUnwrap(URL(string: serverURL), "DROVER_SMOKE_URL must be a URL")
        var request = URLRequest(
            url: baseURL.appendingPathComponent("auth/credentials/\(credentialID)")
        )
        request.httpMethod = "DELETE"
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")

        let (_, response) = try await URLSession.shared.data(for: request)
        let http = try XCTUnwrap(response as? HTTPURLResponse)
        guard http.statusCode == 204 else {
            throw NSError(
                domain: "DroverE2ECredentialCleanup",
                code: http.statusCode,
                userInfo: [NSLocalizedDescriptionKey: "credential revoke returned HTTP \(http.statusCode)"]
            )
        }
    }

    @MainActor
    private func shoot(_ app: XCUIApplication, _ name: String) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
