import SwiftUI
import Testing
import UIKit
@testable import Drover

@MainActor
struct AnalyticsRefreshBannerTests {
    @Test func refreshBannerExposesItsInformationToAssistiveTechnology() {
        let presentation = AnalyticsRefreshBannerPresentation(
            message: "Activity changed; refreshed."
        )
        #expect(presentation.accessibilityLabel == "Activity changed; refreshed.")
        #expect(AnalyticsRefreshBannerPresentation.identifier == "analytics-refresh-notice")

        let host = UIHostingController(rootView: AnalyticsRefreshBanner(
            message: presentation.message
        ))
        let size = host.sizeThatFits(in: CGSize(width: 393, height: 100))
        #expect(size.height > 0)
        #expect(size.width > 0)
    }

    @Test func projectionNoticeAndRetryHaveStableAccessibilityIdentifiers() {
        let presentation = AnalyticsProjectionBannerPresentation(
            message: "Historical activity is catching up (2 of 5 dates complete)."
        )
        #expect(presentation.accessibilityLabel == presentation.message)
        #expect(AnalyticsProjectionBannerPresentation.identifier == "analytics-projection-notice")
        #expect(AnalyticsRetryPresentation.identifier == "analytics-retry")

        let host = UIHostingController(rootView: AnalyticsProjectionBanner(
            message: presentation.message
        ))
        let size = host.sizeThatFits(in: CGSize(width: 393, height: 100))
        #expect(size.height > 0)
        #expect(size.width > 0)
    }
}
