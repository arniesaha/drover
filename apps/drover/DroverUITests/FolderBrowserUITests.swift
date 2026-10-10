import XCTest

/// Uses the isolated synthetic hub, never a saved server connection.
final class FolderBrowserUITests: XCTestCase {
    override func setUpWithError() throws { continueAfterFailure = false }

    @MainActor private func openBrowser() -> XCUIApplication {
        let app = XCUIApplication()
        app.launchEnvironment["DROVER_UI_TEST_SCENARIO"] = "core-journey"
        app.launchEnvironment["DROVER_UI_TEST_RUN_ID"] = UUID().uuidString
        app.launch()
        XCTAssertTrue(app.buttons["launch-button"].waitForExistence(timeout: 5))
        app.buttons["launch-button"].tap()
        XCTAssertTrue(app.buttons["launch-folder-browser"].waitForExistence(timeout: 5))
        app.buttons["launch-folder-browser"].tap()
        return app
    }

    @MainActor private func capture(_ name: String, _ app: XCUIApplication) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    @MainActor func testBrowseFilterBackUpSelectAndManualEntry() {
        let app = openBrowser()
        XCTAssertTrue(app.buttons["Home, folder"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["project, folder"].exists)
        capture("folder-entry-recents-and-roots", app)
        app.buttons["Home, folder"].tap()
        XCTAssertTrue(app.buttons["project, Git repository, folder"].waitForExistence(timeout: 5))
        app.buttons["project, Git repository, folder"].tap()
        XCTAssertTrue(app.buttons["Sources, folder"].waitForExistence(timeout: 5))
        capture("folder-browse-breadcrumb-and-git", app)
        let filter = app.textFields["folder-browser-filter"]
        filter.tap()
        filter.typeText("source")
        XCTAssertTrue(app.buttons["tools, Git repository, folder"].waitForNonExistence(timeout: 5))
        app.buttons["Sources, folder"].tap()
        XCTAssertTrue(app.staticTexts["No folders"].waitForExistence(timeout: 5))
        capture("folder-empty", app)
        app.coordinate(withNormalizedOffset: CGVector(dx: 0.02, dy: 0.5))
            .press(forDuration: 0.05, thenDragTo: app.coordinate(withNormalizedOffset: CGVector(dx: 0.9, dy: 0.5)))
        XCTAssertTrue(app.buttons["Sources, folder"].waitForExistence(timeout: 5))
        app.buttons["folder-browser-up"].tap()
        XCTAssertTrue(app.buttons["project, Git repository, folder"].waitForExistence(timeout: 5))
        app.buttons["project, Git repository, folder"].tap()
        app.buttons["folder-browser-use"].tap()
        XCTAssertTrue(app.buttons["launch-folder-browser"].waitForExistence(timeout: 5))
        XCTAssertEqual(app.buttons["launch-folder-browser"].value as? String, "/fixture/project")
        XCTAssertTrue(app.buttons["Recent working directories"].exists)
        app.buttons["Enter path manually"].tap()
        XCTAssertTrue(app.textFields["launch-manual-cwd"].exists)
    }

    @MainActor func testPermissionAndOfflineStatesDisableSelection() {
        let app = openBrowser()
        XCTAssertTrue(app.buttons["Home, folder"].waitForExistence(timeout: 5))
        app.buttons["Home, folder"].tap()
        XCTAssertTrue(app.buttons["locked, folder"].waitForExistence(timeout: 5))
        app.buttons["locked, folder"].tap()
        XCTAssertTrue(app.staticTexts["folder-browser-error"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["folder-browser-use"].isEnabled)
        capture("folder-permission-denied", app)
        app.navigationBars["Choose folder"].buttons["BackButton"].tap()
        app.buttons["offline, folder"].tap()
        XCTAssertTrue(app.staticTexts["Host offline or unreachable. Try again when it reconnects."].waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["folder-browser-use"].isEnabled)
        capture("folder-host-offline", app)
    }
}
