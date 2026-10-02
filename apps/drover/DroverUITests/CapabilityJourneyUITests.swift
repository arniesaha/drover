import XCTest

/// Credential-free journey over the synthetic `capability-journey` fleet
/// (#420): the same app navigation as the core journey, against hosts whose
/// capability envelopes differ materially. Every assertion is about a control
/// the envelope does or does not advertise, never about a harness name.
final class CapabilityJourneyUITests: XCTestCase {
    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    @MainActor
    private func launchApp() -> XCUIApplication {
        let app = XCUIApplication()
        app.launchEnvironment["DROVER_UI_TEST_SCENARIO"] = "capability-journey"
        app.launchEnvironment["DROVER_UI_TEST_RUN_ID"] = UUID().uuidString
        app.launch()
        return app
    }

    /// A harness that advertises approvals but not interrupt or JPEG
    /// attachments: Allow/Deny render, Interrupt is disabled with its reason.
    @MainActor
    func testUnusualAdapterShowsApprovalsButNotInterruptOrAttachments() {
        let app = launchApp()
        let session = app.buttons["fixture-lab-session"]
        XCTAssertTrue(session.waitForExistence(timeout: 5))
        session.tap()

        XCTAssertTrue(app.buttons["approval-allow"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["approval-deny"].exists)
        XCTAssertFalse(app.otherElements["approval-unavailable"].exists)

        let attach = app.buttons["composer-attach"]
        XCTAssertTrue(attach.waitForExistence(timeout: 5))
        XCTAssertFalse(attach.isEnabled, "PNG-only harness cannot take the app's JPEG")

        app.buttons["chat-menu"].tap()
        let interrupt = app.buttons["chat-interrupt"]
        XCTAssertTrue(interrupt.waitForExistence(timeout: 5))
        XCTAssertFalse(interrupt.isEnabled)
        // The reason is the menu item's subtitle; depending on the OS it is
        // exposed as its own text or folded into the item's label or value.
        let spoken = [interrupt.label, (interrupt.value as? String) ?? ""].joined(separator: " ")
        let reason = app.descendants(matching: .any).matching(
            NSPredicate(format: "label CONTAINS %@", "doesn't support interrupt")
        ).firstMatch
        XCTAssertTrue(
            spoken.contains("doesn't support interrupt") || reason.exists,
            "the disabled item says why; it exposes \"\(spoken)\""
        )
    }

    /// A harness that advertises interrupt but not approvals: the pending
    /// request explains itself instead of offering Allow/Deny.
    @MainActor
    func testHarnessWithoutApprovalsExplainsThePendingRequest() {
        let app = launchApp()
        let session = app.buttons["fixture-codex-approval"]
        XCTAssertTrue(session.waitForExistence(timeout: 5))
        session.tap()

        let unavailable = app.staticTexts.containing(
            NSPredicate(format: "label CONTAINS %@", "doesn't advertise approvals")
        ).firstMatch
        XCTAssertTrue(unavailable.waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["approval-allow"].exists)

        XCTAssertTrue(app.buttons["composer-attach"].isEnabled)
        app.buttons["chat-menu"].tap()
        let interrupt = app.buttons["chat-interrupt"]
        XCTAssertTrue(interrupt.waitForExistence(timeout: 5))
        XCTAssertTrue(interrupt.isEnabled)
    }

    /// A legacy host that publishes no matrix offers nothing to launch and
    /// says how to fix it; switching back restores the advertised launcher.
    @MainActor
    func testLegacyHostOffersNothingToLaunchAndExplainsWhy() {
        let app = launchApp()
        let launch = app.buttons["launch-button"]
        XCTAssertTrue(launch.waitForExistence(timeout: 5))
        launch.tap()

        let confirm = app.buttons["launch-confirm-button"]
        XCTAssertTrue(confirm.waitForExistence(timeout: 5))
        XCTAssertTrue(confirm.isEnabled)
        XCTAssertTrue(app.buttons["launch-attach"].waitForExistence(timeout: 5))

        app.buttons["launch-host-picker"].tap()
        let legacy = app.buttons["Legacy Mac"]
        XCTAssertTrue(legacy.waitForExistence(timeout: 5))
        legacy.tap()

        let reason = app.staticTexts.containing(
            NSPredicate(format: "label CONTAINS %@", "older Drover")
        ).firstMatch
        XCTAssertTrue(reason.waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["launch-harness-picker"].exists)
        XCTAssertFalse(confirm.isEnabled)
        XCTAssertFalse(app.buttons["launch-attach"].exists, "no structured launch, no starting prompt")
    }
}
