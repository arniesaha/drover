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
        // Mac Mini and the work laptop are failing; their chips say so, and
        // the expanded card no longer repeats it as a footer line.
        for footer in ["Couldn't reach", "Not reporting on"] {
            XCTAssertFalse(app.staticTexts.matching(NSPredicate(format: "label BEGINSWITH %@", footer)).firstMatch.exists)
        }
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
    func testStudioFallbackAndOfflineGoogleHostsProduceOneAccount() {
        let app = launch()
        app.tabBars.buttons["Home"].tap()
        let navigation = app.buttons["provider-capacity-navigation"]
        XCTAssertTrue(navigation.waitForExistence(timeout: 8))
        XCTAssertTrue(navigation.label.contains("11 accounts"))
        navigation.tap()
        let scroll = app.scrollViews["provider-accounts-scroll"]
        let googleAccounts = app.buttons.matching(NSPredicate(format: "label CONTAINS %@", "Google · arniesaha@gmail.com"))
        let google = googleAccounts.firstMatch
        for _ in 0..<6 where !google.isHittable { scroll.swipeUp() }
        XCTAssertTrue(google.isHittable)
        XCTAssertEqual(googleAccounts.count, 1)
        XCTAssertFalse(app.buttons.matching(NSPredicate(format: "label CONTAINS %@", "Google · Antigravity")).firstMatch.exists)
        // The stale NAS is a chip in the account's host row, with no
        // separate "Stale hosts" list and no "Not reporting on" footer.
        let nas = app.descendants(matching: .any)["provider-host-google|arniesaha@gmail.com-nas"]
        XCTAssertTrue(nas.exists)
        // Host titles can fall back to lower-case ids on this path.
        XCTAssertTrue(nas.label.lowercased().hasPrefix("nas, stale, last reported 5 days ago"), nas.label)
        let mini = app.descendants(matching: .any)["provider-host-google|arniesaha@gmail.com-mini"]
        XCTAssertTrue(mini.exists)
        XCTAssertFalse(app.buttons.matching(NSPredicate(format: "label CONTAINS %@", "Stale hosts")).firstMatch.exists)
        XCTAssertFalse(app.descendants(matching: .any).matching(NSPredicate(format: "label CONTAINS %@", "Not reporting on")).firstMatch.exists)
        screenshot("google-account-stale-hosts", app)
    }

    /// Reference hub: work-laptop's last probe failed as `unavailable` before
    /// it went dark for 6 days. Its 3%-left reading stays off Home (count and
    /// lowest meter unchanged) and shows as a stale chip on its Accounts card.
    @MainActor
    func testLongDarkUnavailableHostStaysOffHome() {
        let app = launch()
        app.tabBars.buttons["Home"].tap()
        let navigation = app.buttons["provider-capacity-navigation"]
        XCTAssertTrue(navigation.waitForExistence(timeout: 8))
        XCTAssertTrue(navigation.label.contains("11 accounts"))
        // The strip leads with the lowest remaining meter; 3% would lead it.
        let strip = app.scrollViews["account-meter-strip"]
        XCTAssertTrue(strip.exists)
        XCTAssertTrue(strip.buttons.firstMatch.label.contains("account-2@example.com, Anthropic, 19% left"))
        XCTAssertFalse(strip.buttons.matching(NSPredicate(format: "label CONTAINS %@", "work@example.com")).firstMatch.exists)
        navigation.tap()
        let scroll = app.scrollViews["provider-accounts-scroll"]
        let work = app.buttons.matching(NSPredicate(format: "label CONTAINS %@", "Anthropic · work@example.com")).firstMatch
        for _ in 0..<8 where !work.isHittable { scroll.swipeUp() }
        XCTAssertTrue(work.isHittable)
        let laptop = app.descendants(matching: .any)["provider-host-anthropic|work@example.com-work-laptop"]
        XCTAssertTrue(laptop.exists)
        XCTAssertEqual(laptop.label, "work-laptop, stale, last reported 6 days ago, couldn't reach host")
        screenshot("dark-unavailable-host-stale", app)
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
