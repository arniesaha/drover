import XCTest

final class ConfigurationReviewUITests: XCTestCase {
    override func setUpWithError() throws { continueAfterFailure = false }

    @MainActor
    private func openReview(model: Bool, historical: Bool = false, large: Bool = false, failure: String? = nil) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchEnvironment["DROVER_UI_TEST_SCENARIO"] = "observability"
        app.launchEnvironment["DROVER_UI_TEST_RUN_ID"] = UUID().uuidString
        if model { app.launchEnvironment["DROVER_UI_TEST_CONFIGURATION_REVIEW"] = "model" }
        if let failure { app.launchEnvironment["DROVER_UI_TEST_CONFIGURATION_REVIEW"] = failure }
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
        for _ in 0..<4 { app.swipeUp() }
        capture("configuration-model-detail-bottom-light", app)
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
        for _ in 0..<10 where !row.isHittable { app.swipeUp() }
        XCTAssertTrue(row.isHittable)
        capture("configuration-historical-large-dark", app)
        row.tap()
        XCTAssertTrue(app.descendants(matching: .any)["insight-detail-header"].waitForExistence(timeout: 8))
        XCTAssertFalse(app.buttons["insight-check-again"].exists)
        capture("configuration-historical-detail-large-dark", app)
    }

    @MainActor
    func testOfflineFindingsKeepSetupAndBackUsable() {
        let app = openReview(model: false, failure: "offline")
        let error = app.staticTexts["Could not load review findings. Your retained evidence has not been removed. Check the connection and retry."]
        XCTAssertTrue(error.waitForExistence(timeout: 8))
        XCTAssertFalse(app.staticTexts["No AI findings yet"].exists)
        app.buttons["Retry findings"].tap()
        XCTAssertTrue(error.waitForExistence(timeout: 8))
        capture("configuration-offline-light", app)
        app.segmentedControls["configuration-review-tabs"].buttons["Setup & privacy"].tap()
        XCTAssertTrue(app.staticTexts["Last successful review, Not reported"].waitForExistence(timeout: 5))
        capture("configuration-error-setup-light", app)
        app.navigationBars.buttons.element(boundBy: 0).tap()
        XCTAssertTrue(app.buttons["advanced-configuration-review"].waitForExistence(timeout: 5))
    }

    @MainActor
    func testBusyFindingsKeepNavigationUsable() {
        let app = openReview(model: false, failure: "busy")
        XCTAssertTrue(app.staticTexts["The server is busy. Retained findings are unchanged. Try again shortly, or open Setup & privacy."].waitForExistence(timeout: 8))
        XCTAssertFalse(app.staticTexts["No AI findings yet"].exists)
        capture("configuration-busy-light", app)
        app.segmentedControls["configuration-review-tabs"].buttons["Setup & privacy"].tap()
        XCTAssertTrue(app.staticTexts["Last successful review, Not reported"].waitForExistence(timeout: 5))
        capture("configuration-error-setup-light", app)
        app.segmentedControls["configuration-review-tabs"].buttons["Findings"].tap()
        XCTAssertTrue(app.buttons["Retry findings"].waitForExistence(timeout: 8))
    }

    @MainActor
    private func capture(_ name: String, _ app: XCUIApplication) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
