import XCTest

final class ObservabilityFixtureUITests: XCTestCase {
    @MainActor
    func testComparisonsFiltersAndAccountDetailsRemainReachable() {
        let app = launch()
        let hosts = app.buttons["analytics-dimension-hosts"]
        XCTAssertTrue(hosts.waitForExistence(timeout: 5))
        hosts.tap()
        XCTAssertTrue(app.buttons["analytics-rank-toggle-hosts"].exists)
        XCTAssertFalse(app.buttons["analytics-rank-toggle-projects"].exists)
        screenshot("analytics-host-comparison", app)

        app.tabBars.buttons["Insights"].tap()
        XCTAssertTrue(app.staticTexts["1 LOADED FINDING"].waitForExistence(timeout: 5))
        app.buttons.matching(NSPredicate(format: "label CONTAINS %@", "More filters")).firstMatch.tap()
        XCTAssertTrue(app.textFields["Filter insights by host"].exists)
        screenshot("insights-overview", app)

        app.tabBars.buttons["Accounts"].tap()
        let account = app.buttons.matching(NSPredicate(format: "label CONTAINS %@", "alex@example.com")).firstMatch
        XCTAssertTrue(account.waitForExistence(timeout: 5))
        screenshot("accounts-compact", app)
        account.tap()
        XCTAssertTrue(app.staticTexts["Five hour"].exists)
    }

    @MainActor
    func testLongHostNamesRemainOnScreenAtAccessibilitySize() {
        let app = launch(large: true)
        app.tabBars.buttons["Accounts"].tap()
        let host = app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "Work laptop")).firstMatch
        XCTAssertTrue(host.waitForExistence(timeout: 5))
        XCTAssertLessThanOrEqual(host.frame.maxX, app.frame.maxX)
        XCTAssertGreaterThanOrEqual(host.frame.minX, app.frame.minX)
        screenshot("accounts-accessibility", app)
    }

    @MainActor
    func testHomePreviewOpensScrollingAccountsAndReturnsToSessions() {
        let app = launch(dark: true)
        app.tabBars.buttons["Home"].tap()
        let preview = app.descendants(matching: .any)["provider-capacity-summary"]
        XCTAssertTrue(preview.waitForExistence(timeout: 8))
        XCTAssertLessThanOrEqual(preview.frame.height, 90)
        XCTAssertTrue(app.buttons["provider-capacity-navigation"].label.contains("8 additional accounts"))
        XCTAssertGreaterThanOrEqual(app.buttons["provider-capacity-navigation"].frame.height, 44)
        let fleet = app.scrollViews["fleet-list"]
        XCTAssertTrue(fleet.exists)
        XCTAssertGreaterThan(fleet.frame.height, app.frame.height * 0.45)
        XCTAssertTrue(app.buttons["launch-button"].isHittable)
        screenshot("home-compact-account-preview", app)
        let strip = app.scrollViews["account-meter-strip"]
        XCTAssertTrue(strip.exists)
        let leadingAccount = strip.buttons.matching(NSPredicate(format: "label CONTAINS %@", "account-2@example.com")).firstMatch
        XCTAssertTrue(leadingAccount.isHittable)
        let initialX = leadingAccount.frame.minX
        strip.swipeLeft()
        XCTAssertLessThan(leadingAccount.frame.minX, initialX)
        XCTAssertTrue(app.buttons["launch-button"].isHittable)
        screenshot("home-account-preview-scrolled", app)
        app.buttons["provider-capacity-navigation"].tap()
        let scroll = app.scrollViews["provider-accounts-scroll"]
        XCTAssertTrue(scroll.waitForExistence(timeout: 5))
        let account = app.buttons.matching(NSPredicate(format: "label CONTAINS %@", "alex@example.com")).firstMatch
        XCTAssertTrue(account.isHittable)
        account.tap()
        XCTAssertTrue(app.staticTexts["Five hour"].exists)
        screenshot("accounts-dedicated-page", app)
        let openAI = app.staticTexts["provider-heading-openai"]
        for _ in 0..<6 where !openAI.isHittable { scroll.swipeUp() }
        XCTAssertTrue(openAI.isHittable)
        app.navigationBars.buttons.firstMatch.tap()
        XCTAssertTrue(preview.waitForExistence(timeout: 5))
        XCTAssertLessThanOrEqual(preview.frame.height, 90)
        XCTAssertTrue(app.buttons["launch-button"].isHittable)
    }

    @MainActor
    func testHomePreviewKeepsSessionsReachableAtAccessibilitySize() {
        let app = launch(large: true)
        app.tabBars.buttons["Home"].tap()
        let preview = app.descendants(matching: .any)["provider-capacity-summary"]
        XCTAssertTrue(preview.waitForExistence(timeout: 8))
        XCTAssertTrue(app.buttons["lowest-account-meter"].label.contains("account-2@example.com, Anthropic, 19% left"))
        XCTAssertLessThanOrEqual(preview.frame.height, 145)
        XCTAssertGreaterThan(app.scrollViews["fleet-list"].frame.height, 180)
        XCTAssertTrue(app.buttons["launch-button"].isHittable)
        XCTAssertLessThanOrEqual(preview.frame.maxX, app.frame.maxX)
        screenshot("home-preview-accessibility", app)
    }

    @MainActor
    private func launch(large: Bool = false, dark: Bool = false) -> XCUIApplication {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment["DROVER_UI_TEST_SCENARIO"] = "observability"
        app.launchEnvironment["DROVER_UI_TEST_RUN_ID"] = UUID().uuidString
        if large {
            app.launchArguments += ["-UIPreferredContentSizeCategoryName", "UICTContentSizeCategoryAccessibilityXXXL"]
        }
        if dark { app.launchArguments += ["-drover.appearance", "dark"] }
        app.launch()
        return app
    }

    @MainActor
    private func screenshot(_ name: String, _ app: XCUIApplication) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
