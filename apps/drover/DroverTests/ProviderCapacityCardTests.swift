import Foundation
import SwiftUI
import Testing
import UIKit
@testable import Drover
@testable import DroverKit

/// Native layout checks for compact accounts. Rows may grow for long names
/// and accessibility text sizes; quota window count must not change the
/// collapsed row's height.
@MainActor
struct ProviderCapacityCardTests {
    private static let cardWidth: CGFloat = 360
    private static let offeredHeight: CGFloat = 900

    // MARK: - Fixtures

    /// Four windows, a two-line account label, three hosts. The tallest thing
    /// the strip has to render.
    private static let anthropic = """
    {"snapshot_id":"s1","dedup_key":"k1","provider":"anthropic",
     "account_label":"arnab.saha@atlan.com","plan_label":"team",
     "host_id":"work-laptop","status":"ok","observed_at":"2026-08-09T18:00:00Z",
     "source":"claude-oauth-usage",
     "windows":[{"kind":"extra_usage","used_percent":3.6},
                {"kind":"five_hour","used_percent":4,"resets_at":"2026-08-09T20:00:00Z"},
                {"kind":"nimbus_quill","used_percent":0},
                {"kind":"seven_day","used_percent":26,"resets_at":"2026-08-10T08:00:00Z"}]}
    """

    /// One window, and a label short enough to fit on a single line — the
    /// case that used to leave a gap where the second line would have been.
    private static let openai = """
    {"snapshot_id":"s2","dedup_key":"k2","provider":"openai",
     "account_label":"me@example.com","plan_label":"prolite",
     "host_id":"mac-mini","status":"ok","observed_at":"2026-08-09T18:00:00Z",
     "source":"codex-app-server",
     "windows":[{"kind":"primary","used_percent":71,"resets_at":"2026-08-14T18:00:00Z"}]}
    """

    /// No windows at all. The shortest card, and the one that has to prove
    /// "unavailable" is the same shape as "available".
    private static let google = """
    {"snapshot_id":"s3","dedup_key":"k3","provider":"google",
     "account_label":"Antigravity","host_id":"nas","status":"usage_unavailable",
     "observed_at":"2026-08-09T18:00:00Z","source":"harness-inventory","windows":[]}
    """

    /// A probe that failed. Carries a reason line the healthy cards do not.
    private static let errored = """
    {"snapshot_id":"s4","dedup_key":"k4","provider":"openai",
     "account_label":"Codex","host_id":"work-laptop","status":"error",
     "observed_at":"2026-08-09T18:00:00Z","error_category":"cli_not_found",
     "source":"codex-app-server","windows":[]}
    """

    private func height(_ json: String, proposing proposal: CGFloat? = nil) throws -> CGFloat {
        let account = try JSONDecoder().decode(ProviderAccount.self, from: Data(json.utf8))
        let subscription = try #require(
            ProviderSubscriptionGrouping.group(
                [account],
                hostTitles: ["work-laptop": "work-laptop", "mac-mini": "Mac Mini", "nas": "NAS"],
                now: account.observedAt
            ).first
        )
        let card = ProviderAccountCard(
            subscription: subscription,
            section: ProviderSectionPresentation(status: .ok)
        )
        let host = UIHostingController(rootView: card.frame(width: Self.cardWidth).droverTint())
        host.view.frame = CGRect(x: 0, y: 0, width: Self.cardWidth, height: Self.offeredHeight)
        host.view.layoutIfNeeded()
        // Measure the row's content rather than the hosting view's frame.
        return host.sizeThatFits(
            in: CGSize(
                width: Self.cardWidth,
                height: proposal ?? UIView.layoutFittingCompressedSize.height
            )
        ).height
    }

    // MARK: - Tests

    @Test func collapsedAccountDoesNotGrowWithQuotaWindowCount() throws {
        // Use the exact same identity/host, changing only its window inventory.
        let data = try JSONSerialization.jsonObject(with: Data(Self.anthropic.utf8)) as! [String: Any]
        var noWindows = data
        noWindows["windows"] = []
        let emptyJSON = String(data: try JSONSerialization.data(withJSONObject: noWindows), encoding: .utf8)!
        #expect(try height(Self.anthropic) == height(emptyJSON))
    }

    @Test func collapsedAccountStaysCompactForAvailableAndMissingQuota() throws {
        for json in [Self.anthropic, Self.openai, Self.google, Self.errored] {
            #expect(try height(json) <= 190)
        }
    }

    /// The compact row uses the tightest consumption window and shows its
    /// remaining capacity; other windows stay available in the disclosure.
    @Test func headlineSelectsTheTightestConsumptionWindow() throws {
        let account = try JSONDecoder().decode(
            ProviderAccount.self, from: Data(Self.anthropic.utf8)
        )
        let subscription = try #require(
            ProviderSubscriptionGrouping.group([account], now: account.observedAt).first
        )

        #expect(subscription.headline.windowTitle == "Seven day")
        #expect(subscription.headline.fraction == 0.26)
    }
}
