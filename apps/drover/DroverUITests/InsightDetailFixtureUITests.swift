import XCTest

/// A credential-free rendered-screen check for the action-first Insight detail.
final class InsightDetailFixtureUITests: XCTestCase {
    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    @MainActor
    func testInsightDetailKeepsEvidenceCollapsedUntilRequested() {
        let app = XCUIApplication()
        app.launchEnvironment["DROVER_UI_TEST_SCENARIO"] = "insight-detail"
        app.launchEnvironment["DROVER_UI_TEST_RUN_ID"] = UUID().uuidString
        app.launch()

        XCTAssertTrue(app.buttons["insight-check-again"].waitForExistence(timeout: 5))
        let header = app.descendants(matching: .any)["insight-detail-header"]
        XCTAssertTrue(header.waitForExistence(timeout: 5))
        XCTAssertTrue(header.label.contains("Status: Open"))
        XCTAssertTrue(header.label.contains("Evidence: 2 observations"))
        XCTAssertFalse(app.staticTexts["Source reference: fixture-source-a"].exists)
        addScreenshot(named: "insight-detail-standard-collapsed", app: app)

        let evidenceDetails = app.buttons["Show evidence details"]
        let scrollView = app.scrollViews.firstMatch
        for _ in 0..<4 where !evidenceDetails.isHittable {
            scrollView.swipeUp()
        }
        XCTAssertTrue(evidenceDetails.isHittable)
        evidenceDetails.tap()
        XCTAssertTrue(
            app.staticTexts["Source reference: fixture-source-a"].waitForExistence(timeout: 5)
        )
        XCTAssertTrue(app.staticTexts["Host: fixture-host"].exists)
        XCTAssertTrue(app.staticTexts["Missing Value: Not reported"].exists)
        addScreenshot(named: "insight-detail-standard-expanded", app: app)
    }

    @MainActor
    func testHistoricalInsightListAndDetailPreserveFactsWithoutCurrentAlarm() {
        let app = XCUIApplication()
        app.launchEnvironment["DROVER_UI_TEST_SCENARIO"] = "observability"
        app.launchEnvironment["DROVER_UI_TEST_RUN_ID"] = UUID().uuidString
        app.launchEnvironment["DROVER_UI_TEST_HISTORICAL_INSIGHT"] = "1"
        app.launchArguments += ["-drover.appearance", "light"]
        app.launch()
        let inbox = app.tabBars.buttons["Inbox"]
        XCTAssertTrue(inbox.waitForExistence(timeout: 8))
        inbox.tap()
        XCTAssertFalse(app.staticTexts.matching(NSPredicate(format: "label BEGINSWITH %@", "Open priority insights")).firstMatch.exists)
        addScreenshot(named: "historical-insights-no-priority-warning", app: app)
        app.segmentedControls["inbox-segments"].buttons["Insights"].tap()
        XCTAssertTrue(app.staticTexts["No current insights"].waitForExistence(timeout: 8))
        XCTAssertFalse(app.buttons.matching(NSPredicate(format: "label CONTAINS %@", "Fixture hook needs attention")).firstMatch.exists)
        addScreenshot(named: "historical-insights-current-empty", app: app)
        app.segmentedControls["insights-collection"].buttons["Historical"].tap()
        XCTAssertTrue(app.staticTexts["3 loaded findings"].waitForExistence(timeout: 8))
        let row = app.buttons.matching(NSPredicate(format: "label CONTAINS %@", "Fixture hook needs attention")).firstMatch
        XCTAssertTrue(row.waitForExistence(timeout: 8))
        XCTAssertTrue(row.label.contains("Historical evidence"))
        XCTAssertTrue(row.label.contains("Not a current health assessment"))
        XCTAssertTrue(row.label.contains("Recorded severity: High"))
        XCTAssertTrue(row.label.lowercased().contains("open"))
        addScreenshot(named: "historical-insight-list-light", app: app)
        row.tap()
        let header = app.descendants(matching: .any)["insight-detail-header"].firstMatch
        XCTAssertTrue(header.waitForExistence(timeout: 8))
        XCTAssertTrue(header.label.contains("Historical evidence"))
        XCTAssertTrue(header.label.contains("Status: Open"))
        XCTAssertTrue(header.label.contains("Evidence: 2 observations"))
        let obsolete = app.buttons["Obsolete context — not current action"]
        for _ in 0..<4 where !obsolete.isHittable { app.scrollViews.firstMatch.swipeUp() }
        XCTAssertTrue(obsolete.isHittable)
        XCTAssertFalse(app.staticTexts["Recommended steps"].exists)
        addScreenshot(named: "historical-insight-detail-light", app: app)
        obsolete.tap()
        XCTAssertTrue(app.staticTexts["These steps belonged to a retired rule. They are preserved as evidence, not a request to repair current telemetry."].exists)
        addScreenshot(named: "historical-insight-obsolete-context-expanded", app: app)
    }

    @MainActor
    private func addScreenshot(named name: String, app: XCUIApplication) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
