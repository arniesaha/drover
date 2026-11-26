import XCTest

final class ConfigurationReviewUITests: XCTestCase {
    override func setUpWithError() throws { continueAfterFailure = false }

    @MainActor
    private func openReview(model: Bool, historical: Bool = false, large: Bool = false) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchEnvironment["DROVER_UI_TEST_SCENARIO"] = "observability"
        app.launchEnvironment["DROVER_UI_TEST_RUN_ID"] = UUID().uuidString
        if model { app.launchEnvironment["DROVER_UI_TEST_CONFIGURATION_REVIEW"] = "model" }
        if historical { app.launchEnvironment["DROVER_UI_TEST_HISTORICAL_INSIGHT"] = "1" }
        app.launchArguments += ["-drover.appearance", large ? "dark" : "light"]
        if large { app.launchArguments += ["-UIPreferredContentSizeCategoryName", "UICTContentSizeCategoryAccessibilityXXXL"] }
        app.launch()
        let settings = app.tabBars.buttons["Settings"]
        XCTAssertTrue(settings.waitForExistence(timeout: 8))
        settings.tap()
        let advanced = app.buttons["settings-advanced"]
        for _ in 0..<10 where !advanced.isHittable { app.swipeUp() }
        XCTAssertTrue(advanced.isHittable)
        advanced.tap()
        let review = app.buttons["advanced-configuration-review"]
        XCTAssertTrue(review.waitForExistence(timeout: 5))
        review.tap()
        XCTAssertTrue(app.segmentedControls["configuration-review-tabs"].waitForExistence(timeout: 5))
        return app
    }

    @MainActor
    func testDeterministicOnlyResultsAreNotAIFindings() {
        let app = openReview(model: false)
        XCTAssertTrue(app.staticTexts["No AI findings yet"].waitForExistence(timeout: 8))
        capture("configuration-empty-light", app)
        app.buttons["configuration-review-open-setup"].tap()
        XCTAssertTrue(app.descendants(matching: .any)["configuration-review-setup"].waitForExistence(timeout: 5))
        capture("configuration-setup-light", app)
        app.segmentedControls["configuration-review-tabs"].buttons["Findings"].tap()
        XCTAssertTrue(app.staticTexts["No AI findings yet"].waitForExistence(timeout: 8))
        app.navigationBars.buttons.element(boundBy: 0).tap()
        XCTAssertTrue(app.buttons["advanced-configuration-review"].waitForExistence(timeout: 5))
    }

    @MainActor
    func testAIReviewDetailBackAndRootTabReturn() {
        let app = openReview(model: true)
        let row = app.buttons["insight-0123456789abcdef0123456789abcdef"]
        XCTAssertTrue(row.waitForExistence(timeout: 8))
        capture("configuration-model-list-light", app)
        row.tap()
        XCTAssertTrue(app.staticTexts["What was observed"].waitForExistence(timeout: 8))
        XCTAssertTrue(app.staticTexts["Why it matters"].exists)
        capture("configuration-model-detail-light", app)
        app.navigationBars.buttons.element(boundBy: 0).tap()
        XCTAssertTrue(row.waitForExistence(timeout: 8))
        app.tabBars.buttons["Home"].tap()
        app.tabBars.buttons["Settings"].tap()
        XCTAssertTrue(app.segmentedControls["configuration-review-tabs"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.segmentedControls["configuration-review-tabs"].buttons["Findings"].isSelected)
        capture("configuration-root-tab-return", app)
    }

    @MainActor
    func testHistoricalReviewAtLargestText() {
        let app = openReview(model: true, historical: true, large: true)
        app.segmentedControls["configuration-review-collection"].buttons["Historical"].tap()
        let row = app.buttons["insight-0123456789abcdef0123456789abcdef"]
        XCTAssertTrue(row.waitForExistence(timeout: 8))
        XCTAssertTrue(row.label.contains("Historical evidence"))
        capture("configuration-historical-large-dark", app)
        row.tap()
        XCTAssertTrue(app.descendants(matching: .any)["insight-detail-header"].waitForExistence(timeout: 8))
        XCTAssertFalse(app.buttons["insight-check-again"].exists)
        capture("configuration-historical-detail-large-dark", app)
    }

    @MainActor
    private func capture(_ name: String, _ app: XCUIApplication) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
