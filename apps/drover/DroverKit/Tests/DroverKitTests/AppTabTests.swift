import Foundation
import Testing
@testable import DroverKit

@Suite struct AppTabTests {
    @Test func allCasesHaveExpectedMetadata() {
        #expect(AppTab.allCases == [.analytics, .insights, .accounts, .home])

        #expect(AppTab.analytics.title == "Analytics")
        #expect(AppTab.analytics.systemImage == "chart.bar")
        #expect(AppTab.analytics.accessibilityIdentifier == "tab-analytics")

        #expect(AppTab.insights.title == "Insights")
        #expect(AppTab.insights.systemImage == "lightbulb")
        #expect(AppTab.insights.accessibilityIdentifier == "tab-insights")

        #expect(AppTab.accounts.title == "Accounts")
        #expect(AppTab.accounts.systemImage == "person.crop.circle")
        #expect(AppTab.accounts.accessibilityIdentifier == "tab-accounts")

        #expect(AppTab.home.title == "Home")
        #expect(AppTab.home.systemImage == "rectangle.stack")
        #expect(AppTab.home.accessibilityIdentifier == "tab-home")
    }

    @Test func persistenceRoundTrip() {
        let suiteName = "AppTabTests-\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        defer { defaults.removePersistentDomain(forName: suiteName) }

        // Default should be home
        #expect(AppTab.load(from: defaults) == .home)

        // Custom fallback
        #expect(AppTab.load(from: defaults, default: .analytics) == .analytics)

        // Save each tab and verify load
        for tab in AppTab.allCases {
            tab.save(to: defaults)
            #expect(AppTab.load(from: defaults) == tab)
        }

        // Corrupted value falls back
        defaults.set("invalid_tab_name", forKey: AppTab.defaultsKey)
        #expect(AppTab.load(from: defaults) == .home)
        #expect(AppTab.load(from: defaults, default: .insights) == .insights)
    }
}
