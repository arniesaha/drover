import XCTest

@MainActor
final class DemoModeUITests: XCTestCase {
    private let sessionStreaming = "demo-session-streaming"
    private let sessionApproval = "demo-session-approval"
    private let sessionDiffArtifacts = "demo-session-diff-artifacts"

    override func setUpWithError() throws {
        continueAfterFailure = false
        addUIInterruptionMonitor(withDescription: "System Alerts") { element in
            MainActor.assumeIsolated {
                for label in ["Allow", "OK"] {
                    let button = element.buttons[label]
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
    private func makeApp() -> XCUIApplication {
        let app = XCUIApplication()
        app.launchEnvironment["DROVER_UI_TEST_SCENARIO"] = "core-journey"
        app.launchEnvironment["DROVER_UI_TEST_RUN_ID"] = UUID().uuidString
        app.launchEnvironment["DROVER_UI_TEST_START_UNPAIRED"] = "1"
        return app
    }

    @MainActor
    private func selectTab(title: String, id: String, in app: XCUIApplication) {
        let tabButton = app.tabBars.buttons[title].firstMatch
        if tabButton.waitForExistence(timeout: 2) {
            tabButton.tap()
            return
        }
        let btn = app.buttons[title].firstMatch
        if btn.waitForExistence(timeout: 2) {
            btn.tap()
            return
        }
        let identButton = app.buttons[id].firstMatch
        if identButton.waitForExistence(timeout: 2) {
            identButton.tap()
            return
        }
        let desc = app.descendants(matching: .any)[id].firstMatch
        if desc.waitForExistence(timeout: 2) {
            desc.tap()
            return
        }
        let staticText = app.staticTexts[title].firstMatch
        if staticText.waitForExistence(timeout: 2) {
            staticText.tap()
        }
    }

    @MainActor
    private func captureScreenshot(named name: String, in app: XCUIApplication) {
        let screenshot = app.screenshot()
        let attachment = XCTAttachment(screenshot: screenshot)
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)

        let isPad = UIDevice.current.userInterfaceIdiom == .pad
        let deviceFolder = isPad ? "ipad-13" : "iphone-6.9"
        let outDir = URL(fileURLWithPath: "/tmp/drover-demo-screenshots/\(deviceFolder)")
        try? FileManager.default.createDirectory(at: outDir, withIntermediateDirectories: true)
        let fileURL = outDir.appendingPathComponent("\(name).png")
        try? screenshot.pngRepresentation.write(to: fileURL)
    }

    @MainActor
    private func signalAndCapture(name: String, app: XCUIApplication) {
        let shotsDir = URL(fileURLWithPath: "/tmp/drover-demo-mode-shots")
        try? FileManager.default.createDirectory(at: shotsDir, withIntermediateDirectories: true)
        let readyFile = shotsDir.appendingPathComponent("ready_\(name).txt")
        let doneFile = shotsDir.appendingPathComponent("done_\(name).txt")
        try? "ready".write(to: readyFile, atomically: true, encoding: .utf8)

        let deadline = Date().addingTimeInterval(3.0)
        while Date() < deadline && !FileManager.default.fileExists(atPath: doneFile.path) {
            RunLoop.current.run(until: Date().addingTimeInterval(0.1))
        }

        let screenshot = app.screenshot()
        let directFile = shotsDir.appendingPathComponent("\(name).png")
        if !FileManager.default.fileExists(atPath: directFile.path) {
            try? screenshot.pngRepresentation.write(to: directFile)
        }
    }

    @MainActor
    func testDemoModeFullJourney() throws {
        let app = makeApp()
        app.launch()

        // 1. Onboarding Screen
        let tryDemoButton = app.buttons["onboarding-try-demo-button"]
        XCTAssertTrue(tryDemoButton.waitForExistence(timeout: 5), "Expected 'Try a demo' button on welcome screen")
        captureScreenshot(named: "01_Onboarding_Welcome", in: app)

        // Tap Try a demo
        tryDemoButton.tap()

        // 2. Fleet / Home Screen in Demo Mode
        let demoBadge = app.descendants(matching: .any)["demo-badge"]
        XCTAssertTrue(demoBadge.waitForExistence(timeout: 5), "Expected DEMO badge on home screen")
        XCTAssertTrue(app.buttons[sessionStreaming].waitForExistence(timeout: 5))
        captureScreenshot(named: "02_Fleet_Home_Demo", in: app)

        // 3. Open Streaming Session
        let streamingSession = app.buttons[sessionStreaming]
        streamingSession.tap()

        let chatTitle = app.staticTexts["chat-recap-title"]
        XCTAssertTrue(chatTitle.waitForExistence(timeout: 5))
        let validationText = app.staticTexts.containing(NSPredicate(format: "label CONTAINS 'checkout validation'")).firstMatch
        _ = validationText.waitForExistence(timeout: 3)

        // Assert no reconnecting pill or reconnecting indicator
        let reconnectingPill = app.descendants(matching: .any)["chat-reconnecting"]
        XCTAssertFalse(reconnectingPill.exists, "Demo session should never show reconnecting pill")
        let reconnectingText = app.staticTexts["Reconnecting"]
        XCTAssertFalse(reconnectingText.exists, "Demo session should never show reconnecting status")

        signalAndCapture(name: "04_demo_session_chat", app: app)

        // Test sending a follow-up turn in demo mode
        let composer = app.textFields["composer-input"]
        if composer.waitForExistence(timeout: 3) {
            composer.tap()
            composer.typeText("Check unit tests")
            let sendButton = app.buttons["composer-send"]
            if sendButton.waitForExistence(timeout: 3) {
                sendButton.tap()
                let simulatedReply = app.staticTexts.containing(NSPredicate(format: "label CONTAINS 'Demo mode: replies are simulated'")).firstMatch
                XCTAssertTrue(simulatedReply.waitForExistence(timeout: 5), "Expected simulated demo reply")
            }
        }
        XCTAssertFalse(reconnectingPill.exists)
        XCTAssertFalse(reconnectingText.exists)

        signalAndCapture(name: "04b_demo_session_chat_sent", app: app)

        captureScreenshot(named: "03_Session_Streaming", in: app)

        // Back to fleet
        app.navigationBars.firstMatch.buttons.firstMatch.tap()
        XCTAssertTrue(demoBadge.waitForExistence(timeout: 5))

        // 4. Open Approval Request Session (lives in Inbox in 1.0 IA)
        let openInbox = app.buttons["home-open-inbox"]
        if openInbox.waitForExistence(timeout: 2) && openInbox.isHittable {
            openInbox.tap()
        } else {
            selectTab(title: "Inbox", id: "tab-inbox", in: app)
        }

        let approvalSession = app.buttons["inbox-session-\(sessionApproval)"].exists
            ? app.buttons["inbox-session-\(sessionApproval)"]
            : app.buttons[sessionApproval]
        XCTAssertTrue(approvalSession.waitForExistence(timeout: 5))
        approvalSession.tap()

        let allowButton = app.buttons["approval-allow"]
        XCTAssertTrue(allowButton.waitForExistence(timeout: 5), "Expected 'Allow once' button")
        captureScreenshot(named: "04_Session_ApprovalPrompt", in: app)

        // Approve the request
        allowButton.tap()
        XCTAssertTrue(app.staticTexts["Migration 004 applied successfully. 3 tables and index created."].waitForExistence(timeout: 5))
        captureScreenshot(named: "05_Session_Approved", in: app)

        // Back to inbox/fleet
        let inboxDismiss = app.buttons["Inbox"]
        if inboxDismiss.waitForExistence(timeout: 2) && inboxDismiss.isHittable {
            inboxDismiss.tap()
        } else {
            app.navigationBars.firstMatch.buttons.firstMatch.tap()
        }

        // 5. Open Diff & Artifacts Session (on Home)
        selectTab(title: "Home", id: "tab-home", in: app)
        XCTAssertTrue(demoBadge.waitForExistence(timeout: 5))

        let diffSession = app.buttons[sessionDiffArtifacts]
        if diffSession.waitForExistence(timeout: 3) {
            diffSession.tap()
            _ = app.descendants(matching: .any)["chat-floating-artifacts"].waitForExistence(timeout: 3)
            captureScreenshot(named: "06_Session_DiffArtifacts", in: app)
            app.navigationBars.firstMatch.buttons.firstMatch.tap()
            XCTAssertTrue(demoBadge.waitForExistence(timeout: 5))
        }

        // 6. View Analytics
        selectTab(title: "Analytics", id: "tab-analytics", in: app)

        let analyticsFilters = app.descendants(matching: .any)["analytics-filters"]
        XCTAssertTrue(analyticsFilters.waitForExistence(timeout: 5))
        captureScreenshot(named: "07_Analytics_Overview", in: app)

        // 7. Settings & Exit Demo
        selectTab(title: "Settings", id: "tab-settings", in: app)

        let settingsDemoBadge = app.descendants(matching: .any)["settings-demo-badge"]
        XCTAssertTrue(settingsDemoBadge.waitForExistence(timeout: 5))

        // Trigger sample notification preview
        let previewNotificationButton = app.buttons["settings-preview-notification-button"]
        XCTAssertTrue(previewNotificationButton.waitForExistence(timeout: 5))
        previewNotificationButton.tap()

        let notificationSentLabel = app.descendants(matching: .any)["settings-notification-sent-label"]
        _ = notificationSentLabel.waitForExistence(timeout: 3)
        captureScreenshot(named: "08_Settings_DemoMode", in: app)

        // Exit Demo
        let exitDemoButton = app.buttons["settings-exit-demo-button"]
        XCTAssertTrue(exitDemoButton.waitForExistence(timeout: 5))
        exitDemoButton.tap()

        // 8. Verify back at onboarding
        let welcomeTryDemo = app.buttons["onboarding-try-demo-button"]
        XCTAssertTrue(welcomeTryDemo.waitForExistence(timeout: 5), "Expected to return to Onboarding welcome screen")
        captureScreenshot(named: "09_Returned_To_Onboarding", in: app)
    }
}
