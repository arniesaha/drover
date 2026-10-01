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
    private func launch(large: Bool = false) -> XCUIApplication {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment["DROVER_UI_TEST_SCENARIO"] = "observability"
        app.launchEnvironment["DROVER_UI_TEST_RUN_ID"] = UUID().uuidString
        if large {
            app.launchArguments += ["-UIPreferredContentSizeCategoryName", "UICTContentSizeCategoryAccessibilityXXXL"]
        }
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
