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

    private func addScreenshot(named name: String, app: XCUIApplication) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
