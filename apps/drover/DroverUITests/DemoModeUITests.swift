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
        captureScreenshot(named: "03_Session_Streaming", in: app)

        // Back to fleet
        app.navigationBars.firstMatch.buttons.firstMatch.tap()
        XCTAssertTrue(demoBadge.waitForExistence(timeout: 5))

        // 4. Open Approval Request Session
        let approvalSession = app.buttons[sessionApproval]
        XCTAssertTrue(approvalSession.waitForExistence(timeout: 5))
        approvalSession.tap()

        let allowButton = app.buttons["approval-allow"]
        XCTAssertTrue(allowButton.waitForExistence(timeout: 5), "Expected 'Allow once' button")
        captureScreenshot(named: "04_Session_ApprovalPrompt", in: app)

        // Approve the request
        allowButton.tap()
        XCTAssertTrue(app.staticTexts["Migration 004 applied successfully. 3 tables and index created."].waitForExistence(timeout: 5))
        captureScreenshot(named: "05_Session_Approved", in: app)

        // Back to fleet
        app.navigationBars.firstMatch.buttons.firstMatch.tap()
        XCTAssertTrue(demoBadge.waitForExistence(timeout: 5))

        // 5. Open Diff & Artifacts Session
        let diffSession = app.buttons[sessionDiffArtifacts]
        if diffSession.waitForExistence(timeout: 3) {
            diffSession.tap()
            _ = app.descendants(matching: .any)["chat-floating-artifacts"].waitForExistence(timeout: 3)
            captureScreenshot(named: "06_Session_DiffArtifacts", in: app)
            app.navigationBars.firstMatch.buttons.firstMatch.tap()
            XCTAssertTrue(demoBadge.waitForExistence(timeout: 5))
        }

        // 6. View Analytics
        let analyticsButton = app.buttons["analytics-summary"]
        if !analyticsButton.isHittable {
            app.scrollViews["fleet-list"].swipeUp()
        }
        XCTAssertTrue(analyticsButton.waitForExistence(timeout: 5))
        analyticsButton.tap()

        let analyticsFilters = app.descendants(matching: .any)["analytics-filters"]
        XCTAssertTrue(analyticsFilters.waitForExistence(timeout: 5))
        captureScreenshot(named: "07_Analytics_Overview", in: app)

        // Back to fleet
        app.navigationBars.firstMatch.buttons.firstMatch.tap()
        XCTAssertTrue(demoBadge.waitForExistence(timeout: 5))

        // 7. Settings & Exit Demo
        let settingsButton = app.buttons["settings-button"]
        if !settingsButton.isHittable {
            app.scrollViews["fleet-list"].swipeDown()
        }
        XCTAssertTrue(settingsButton.waitForExistence(timeout: 5))
        settingsButton.tap()

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
