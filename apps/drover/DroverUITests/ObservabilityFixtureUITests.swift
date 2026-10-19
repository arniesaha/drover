import XCTest

final class ObservabilityFixtureUITests: XCTestCase {
    @MainActor
    func testComparisonsFiltersAndAccountDetailsRemainReachable() {
        let app = launch()
        app.buttons["dock-analytics"].tap()
        let hosts = app.buttons["analytics-dimension-hosts"]
        XCTAssertTrue(hosts.waitForExistence(timeout: 5))
        hosts.tap()
        let rankToggle = app.segmentedControls["analytics-rank-toggle-hosts"]
        XCTAssertTrue(rankToggle.waitForExistence(timeout: 5))
        XCTAssertFalse(app.segmentedControls["analytics-rank-toggle-projects"].exists)
        screenshot("analytics-host-comparison", app)

        rankToggle.buttons["Tokens"].tap()
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "ranked by tokens")).firstMatch.waitForExistence(timeout: 2))
        screenshot("analytics-host-comparison-by-tokens", app)
        rankToggle.buttons["Sessions"].tap()

        app.buttons["dock-inbox"].tap()
        app.segmentedControls["inbox-segments"].buttons["Insights"].tap()
        XCTAssertTrue(app.descendants(matching: .any)["insights-filters"].waitForExistence(timeout: 5))
        app.buttons.matching(NSPredicate(format: "label CONTAINS %@", "More filters")).firstMatch.tap()
        XCTAssertTrue(app.textFields["Filter insights by host"].exists)
        screenshot("insights-overview", app)

        app.buttons["dock-accounts"].tap()
        let account = app.buttons.matching(NSPredicate(format: "label CONTAINS %@", "sample.user@example.com")).firstMatch
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
    func testAnalyticsFiltersAndMetricsAtLargestTextSize() {
        checkAnalyticsLargestText(dark: false)
    }

    @MainActor
    func testAnalyticsFiltersAndMetricsAtLargestTextSizeInDarkMode() {
        checkAnalyticsLargestText(dark: true)
    }

    @MainActor
    private func checkAnalyticsLargestText(dark: Bool) {
        let app = launch(large: true, dark: dark)
        app.buttons["dock-analytics"].tap()
        let filters = app.descendants(matching: .any)["analytics-filters"].firstMatch
        XCTAssertTrue(filters.waitForExistence(timeout: 8))
        let days = app.buttons["7 days"].firstMatch
        let toggle = app.buttons["Filters"].firstMatch
        XCTAssertTrue(days.isHittable)
        XCTAssertTrue(toggle.isHittable)
        XCTAssertTrue(app.frame.contains(days.frame))
        XCTAssertTrue(app.frame.contains(toggle.frame))
        XCTAssertFalse(days.frame.intersects(toggle.frame))
        toggle.tap()
        let scroll = app.scrollViews.firstMatch
        for _ in 0..<8 {
            if app.buttons["analytics-dimension-models"].isHittable { break }
            scroll.swipeUp()
        }
        let models = app.buttons["analytics-dimension-models"]
        XCTAssertTrue(models.isHittable, "Analytics must scroll through expanded filters and totals")
        XCTAssertTrue(app.frame.contains(models.frame))
        screenshot(dark ? "analytics-largest-text-dark" : "analytics-largest-text-light", app)
    }

    @MainActor
    func testLongHostNamesRemainOnScreenAtAccessibilitySize() {
        let app = launch(large: true)
        app.buttons["dock-accounts"].tap()
        let host = app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "Work laptop")).firstMatch
        XCTAssertTrue(host.waitForExistence(timeout: 5))
        XCTAssertLessThanOrEqual(host.frame.maxX, app.frame.maxX)
        XCTAssertGreaterThanOrEqual(host.frame.minX, app.frame.minX)
        screenshot("accounts-accessibility", app)
    }

    @MainActor
    func testRealAccountPayloadHasNoHomeCardOrBannerAndAccountsBadge() {
        let app = launch(dark: true, declutter: true)
        let home = app.buttons["dock-home"]
        XCTAssertTrue(home.waitForExistence(timeout: 8))
        home.tap()
        let fleet = app.scrollViews["fleet-list"]
        XCTAssertTrue(fleet.waitForExistence(timeout: 8))
        XCTAssertGreaterThan(fleet.frame.height, app.frame.height * 0.45)
        XCTAssertFalse(app.descendants(matching: .any)["provider-capacity-summary"].exists)
        XCTAssertFalse(app.descendants(matching: .any)["account-health-warning"].exists)
        XCTAssertTrue(app.buttons["launch-button"].isHittable)

        let accountsDock = app.buttons["dock-accounts"]
        XCTAssertTrue(accountsDock.label.contains("1 account needs attention"))
        screenshot("home-scrolled-behind-floating-dock-dark", app)

        accountsDock.tap()
        let scroll = app.scrollViews["provider-accounts-scroll"]
        XCTAssertTrue(scroll.waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts["accounts-needs-attention"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "OpenAI · 12% left")).firstMatch.exists)
        screenshot("accounts-attention-badge-dark", app)
    }

    @MainActor
    func testDeclutterFixtureLightReduceTransparencyScreenshots() {
        let app = launch(declutter: true, reduceTransparency: true)
        let fleet = app.scrollViews["fleet-list"]
        XCTAssertTrue(fleet.waitForExistence(timeout: 8))
        for _ in 0..<3 { fleet.swipeUp() }
        XCTAssertTrue(app.buttons["launch-button"].isHittable)
        screenshot("home-scrolled-floating-dock-light-reduce-transparency", app)

        app.buttons["dock-accounts"].tap()
        XCTAssertTrue(app.staticTexts["accounts-needs-attention"].waitForExistence(timeout: 5))
        screenshot("accounts-attention-badge-light-reduce-transparency", app)
    }

    @MainActor
    func testLastHomeRowReachesAboveFloatingDockAndNewSession() {
        let app = launch(fleetFilters: true)
        app.buttons["dock-home"].tap()
        let fleet = app.scrollViews["fleet-list"]
        XCTAssertTrue(fleet.waitForExistence(timeout: 8))
        for _ in 0..<8 { fleet.swipeUp() }
        let lastRow = app.buttons["gpu-models-input"]
        XCTAssertTrue(lastRow.waitForExistence(timeout: 5))
        XCTAssertTrue(lastRow.isHittable)
        XCTAssertLessThan(lastRow.frame.maxY, app.buttons["launch-button"].frame.minY)
        screenshot("home-last-row-above-floating-dock-light", app)
    }

    @MainActor
    func testStudioFallbackAndOfflineGoogleHostsProduceOneAccount() {
        let app = launch()
        app.buttons["dock-accounts"].tap()
        let scroll = app.scrollViews["provider-accounts-scroll"]
        XCTAssertTrue(scroll.waitForExistence(timeout: 8))
        let googleAccounts = app.buttons.matching(NSPredicate(format: "label CONTAINS %@", "Google · sample.google@example.com"))
        let google = googleAccounts.firstMatch
        for _ in 0..<6 where !google.isHittable { scroll.swipeUp() }
        XCTAssertTrue(google.isHittable)
        XCTAssertEqual(googleAccounts.count, 1)
        XCTAssertFalse(app.buttons.matching(NSPredicate(format: "label CONTAINS %@", "Google · Antigravity")).firstMatch.exists)
        // The stale NAS is a chip in the account's host row, with no
        // separate "Stale hosts" list and no "Not reporting on" footer.
        let nas = app.descendants(matching: .any)["provider-host-google|sample.google@example.com-nas"]
        XCTAssertTrue(nas.exists)
        // Host titles can fall back to lower-case ids on this path.
        XCTAssertTrue(nas.label.lowercased().hasPrefix("nas, stale, last reported 5 days ago"), nas.label)
        let mini = app.descendants(matching: .any)["provider-host-google|sample.google@example.com-mini"]
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
        app.buttons["dock-accounts"].tap()
        let scroll = app.scrollViews["provider-accounts-scroll"]
        XCTAssertTrue(scroll.waitForExistence(timeout: 8))
        let work = app.buttons.matching(NSPredicate(format: "label CONTAINS %@", "Anthropic · work@example.com")).firstMatch
        for _ in 0..<8 where !work.isHittable { scroll.swipeUp() }
        XCTAssertTrue(work.isHittable)
        let laptop = app.descendants(matching: .any)["provider-host-anthropic|work@example.com-work-laptop"]
        XCTAssertTrue(laptop.exists)
        XCTAssertEqual(laptop.label, "work-laptop, stale, last reported 6 days ago, couldn't reach host")
        screenshot("dark-unavailable-host-stale", app)
    }

    @MainActor
    func testHomeFleetOverviewAndFiltering() {
        let app = launch(dark: true, fleetFilters: true)
        app.buttons["dock-home"].tap()

        // 1. Host chips
        let allChip = app.buttons["host-chip-all"]
        XCTAssertTrue(allChip.waitForExistence(timeout: 8))
        XCTAssertTrue(allChip.label.contains("4 active sessions"), "allChip.label was actually: '\(allChip.label)'")

        let studioChip = app.buttons["host-chip-studio"]
        XCTAssertTrue(studioChip.exists)
        XCTAssertTrue(studioChip.label.contains("Arnabs-Mac-Studio"))

        let gpuChip = app.buttons["host-chip-gpu-pc"]
        XCTAssertTrue(gpuChip.exists)
        XCTAssertTrue(gpuChip.label.contains("GPU PC"))

        let nasChip = app.buttons["host-chip-nas"]
        XCTAssertTrue(nasChip.exists)
        XCTAssertTrue(nasChip.label.contains("NAS"))

        screenshot("home-fleet-header-dark", app)

        // 2. Expand Bird's-Eye Overview
        let overviewToggle = app.buttons["overview-toggle"]
        XCTAssertTrue(overviewToggle.waitForExistence(timeout: 5))
        overviewToggle.tap()

        let birdsEye = app.descendants(matching: .any)["fleet-birds-eye-view"]
        XCTAssertTrue(birdsEye.waitForExistence(timeout: 5))
        let summary = app.staticTexts["birds-eye-summary"]
        XCTAssertTrue(summary.exists)
        XCTAssertTrue(summary.label.contains("4 active · 2 live"))

        screenshot("home-fleet-overview-dark", app)

        // 3. Tap matrix cell: drover on GPU PC
        let cell = app.buttons["overview-cell-arniesaha/drover-gpu-pc"]
        XCTAssertTrue(cell.waitForExistence(timeout: 5))
        cell.tap()

        // 4. Verify Active Filters Banner and filtered session rows
        let banner = app.descendants(matching: .any)["active-filters-banner"]
        XCTAssertTrue(banner.waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "GPU PC · arniesaha/drover")).firstMatch.exists)

        XCTAssertTrue(app.buttons["gpu-drover-running"].waitForExistence(timeout: 3))
        XCTAssertFalse(app.buttons["studio-drover-approval"].exists)
        XCTAssertFalse(app.buttons["gpu-models-input"].exists)

        screenshot("home-filtered-host-project", app)

        // 5. Clear filter
        let clearButton = app.buttons["clear-filters-button"]
        XCTAssertTrue(clearButton.exists)
        clearButton.tap()

        XCTAssertFalse(banner.exists)
        overviewToggle.tap() // Collapse overview so session list has full viewport
        XCTAssertTrue(app.buttons["studio-drover-approval"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["gpu-drover-running"].exists)

        // 6. Filter by host chip alone
        studioChip.tap()
        XCTAssertTrue(banner.waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["studio-drover-approval"].waitForExistence(timeout: 3))
        XCTAssertFalse(app.buttons["gpu-drover-running"].exists)

        // Clear host filter by tapping All chip
        allChip.tap()
        XCTAssertFalse(banner.exists)
    }

    @MainActor
    func testHomeFleetOverviewLightMode() {
        let app = launch(dark: false, fleetFilters: true)
        app.buttons["dock-home"].tap()

        let allChip = app.buttons["host-chip-all"]
        XCTAssertTrue(allChip.waitForExistence(timeout: 10))

        let overviewToggle = app.buttons["overview-toggle"]
        XCTAssertTrue(overviewToggle.waitForExistence(timeout: 8))
        overviewToggle.tap()

        let birdsEye = app.descendants(matching: .any)["fleet-birds-eye-view"]
        XCTAssertTrue(birdsEye.waitForExistence(timeout: 5))

        screenshot("home-fleet-overview-light", app)
    }

    @MainActor
    private func launch(
        large: Bool = false, dark: Bool = false, fleetFilters: Bool = false,
        declutter: Bool = false, reduceTransparency: Bool = false
    ) -> XCUIApplication {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment["DROVER_UI_TEST_SCENARIO"] = "observability"
        app.launchEnvironment["DROVER_UI_TEST_RUN_ID"] = UUID().uuidString
        if fleetFilters {
            app.launchEnvironment["DROVER_UI_TEST_FLEET_FILTERS"] = "1"
        }
        if declutter { app.launchEnvironment["DROVER_UI_TEST_HOME_DECLUTTER"] = "1" }
        if reduceTransparency { app.launchEnvironment["DROVER_UI_TEST_REDUCE_TRANSPARENCY"] = "1" }
        if large {
            app.launchArguments += ["-UIPreferredContentSizeCategoryName", "UICTContentSizeCategoryAccessibilityXXXL"]
        }
        if dark { app.launchArguments += ["-drover.appearance", "dark"] }
        app.launch()
        return app
    }

    @MainActor
    private func launchRealApp(large: Bool = false, dark: Bool = false, pendingSessionID: String? = nil) -> XCUIApplication {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment["DROVER_UI_TEST_SCENARIO"] = "core-journey"
        app.launchEnvironment["DROVER_UI_TEST_RUN_ID"] = UUID().uuidString
        if let pendingSessionID {
            app.launchEnvironment["DROVER_UI_TEST_PENDING_SESSION_ID"] = pendingSessionID
        }
        if large {
            app.launchArguments += ["-UIPreferredContentSizeCategoryName", "UICTContentSizeCategoryAccessibilityXXXL"]
        }
        if dark { app.launchArguments += ["-drover.appearance", "dark"] }
        app.launch()
        return app
    }

    @MainActor
    func testRealAppRootTabNavigationAndDeepLink() {
        // 1. Launch against real app root (core-journey scenario)
        let app = launchRealApp()
        // 2. Verify all four dock destinations exist
        let homeTab = app.buttons["dock-home"]
        let analyticsTab = app.buttons["dock-analytics"]
        let inboxTab = app.buttons["dock-inbox"]
        let accountsTab = app.buttons["dock-accounts"]

        XCTAssertTrue(homeTab.exists)
        XCTAssertTrue(analyticsTab.exists)
        XCTAssertTrue(inboxTab.exists)
        XCTAssertTrue(accountsTab.exists)

        // 3. Switch through all four tabs
        analyticsTab.tap()
        XCTAssertTrue(app.navigationBars["Analytics"].waitForExistence(timeout: 5) || app.descendants(matching: .any)["analytics-filters"].waitForExistence(timeout: 5))

        inboxTab.tap()
        XCTAssertTrue(app.navigationBars["Inbox"].waitForExistence(timeout: 5))
        let segments = app.segmentedControls["inbox-segments"]
        XCTAssertTrue(segments.buttons["Needs you · 0"].exists || segments.buttons["Needs you · 1"].exists)
        segments.buttons["Insights"].tap()
        XCTAssertTrue(app.descendants(matching: .any)["insights-filters"].waitForExistence(timeout: 5))
        segments.buttons["History"].tap()
        XCTAssertTrue(app.descendants(matching: .any)["history-list"].waitForExistence(timeout: 5))

        accountsTab.tap()
        XCTAssertTrue(app.navigationBars["Accounts"].waitForExistence(timeout: 5))

        homeTab.tap()
        let fleetList = app.scrollViews["fleet-list"]
        XCTAssertTrue(fleetList.waitForExistence(timeout: 8))

        // 4. Verify dock does not cover the last list row or New Session button
        let newSessionButton = app.buttons["launch-button"]
        XCTAssertTrue(newSessionButton.waitForExistence(timeout: 5))
        XCTAssertTrue(newSessionButton.isHittable)

        screenshot("real-app-home-dock-light", app)
    }

    @MainActor
    func testInboxSegmentsProduceReviewScreenshots() {
        let app = launch()
        app.buttons["dock-home"].tap()
        XCTAssertTrue(app.scrollViews["fleet-list"].waitForExistence(timeout: 8))
        screenshot("nav-home", app)

        let inbox = app.buttons["dock-inbox"]
        inbox.tap()
        let segments = app.segmentedControls["inbox-segments"]
        XCTAssertTrue(segments.waitForExistence(timeout: 5))
        screenshot("nav-inbox-needs-you-badge", app)

        segments.buttons["Insights"].tap()
        XCTAssertTrue(app.descendants(matching: .any)["insights-filters"].waitForExistence(timeout: 5))
        screenshot("nav-inbox-insights", app)

        segments.buttons["History"].tap()
        XCTAssertTrue(app.descendants(matching: .any)["history-list"].waitForExistence(timeout: 5))
        screenshot("nav-inbox-history", app)
    }

    @MainActor
    func testRealAppHomeDockDarkMode() {
        let app = launchRealApp(dark: true)
        let homeTab = app.buttons["dock-home"]
        XCTAssertTrue(homeTab.waitForExistence(timeout: 8))
        let newSessionButton = app.buttons["launch-button"]
        XCTAssertTrue(newSessionButton.waitForExistence(timeout: 5))
        screenshot("real-app-home-dock-dark", app)
    }

    @MainActor
    func testRealAppHomeDockLargestText() {
        let app = launchRealApp(large: true, dark: true)
        let homeTab = app.buttons["dock-home"]
        XCTAssertTrue(homeTab.waitForExistence(timeout: 8))
        let newSessionButton = app.buttons["launch-button"]
        XCTAssertTrue(newSessionButton.waitForExistence(timeout: 5))
        screenshot("real-app-home-dock-largest-text", app)
    }

    @MainActor
    func testRealAppIPadDock() {
        let app = launchRealApp(dark: true)
        let homeTab = app.buttons["dock-home"]
        XCTAssertTrue(homeTab.waitForExistence(timeout: 10))
        screenshot("real-app-ipad-dock", app)
    }

    @MainActor
    func testDeepLinkOpensSessionDirectly() {
        let app = launchRealApp(pendingSessionID: "fixture-session")
        let chatMenu = app.buttons["chat-menu"]
        let codexBar = app.navigationBars["Codex"]
        XCTAssertTrue(chatMenu.waitForExistence(timeout: 10) || codexBar.waitForExistence(timeout: 10))
    }

    @MainActor
    func testOverviewKeepsHostChipsClearOfFleetContent() {
        let app = launch(dark: true, fleetFilters: true)
        let homeTab = app.buttons["dock-home"]
        XCTAssertTrue(homeTab.waitForExistence(timeout: 8))
        homeTab.tap()

        let allChip = app.buttons["host-chip-all"]
        XCTAssertTrue(allChip.waitForExistence(timeout: 10))

        let overviewToggle = app.buttons["overview-toggle"]
        XCTAssertTrue(overviewToggle.waitForExistence(timeout: 8))
        overviewToggle.tap()

        let hostChips = app.buttons["host-chip-all"]
        XCTAssertTrue(hostChips.waitForExistence(timeout: 5))
        XCTAssertFalse(app.descendants(matching: .any)["provider-capacity-summary"].exists)

        screenshot("overview-no-clipping-dark", app)
    }

    @MainActor
    private func screenshot(_ name: String, _ app: XCUIApplication) {
        let screenshot = app.screenshot()
        let attachment = XCTAttachment(screenshot: screenshot)
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)

        let targetDir = URL(fileURLWithPath: "/tmp/drover-fleet-filters")
        try? FileManager.default.createDirectory(at: targetDir, withIntermediateDirectories: true)
        let targetFile = targetDir.appendingPathComponent("\(name).png")
        try? screenshot.pngRepresentation.write(to: targetFile)
    }
}
