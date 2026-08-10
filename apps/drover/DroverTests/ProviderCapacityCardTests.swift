import Foundation
import SwiftUI
import Testing
import UIKit
@testable import Drover
@testable import DroverKit

/// The promise the redesign makes: every provider card is the same card.
///
/// The strip used to render one text block per quota window, so a four-window
/// Anthropic subscription stood roughly four times taller than a Gemini
/// subscription that reported none, beside a one-window OpenAI subscription
/// somewhere in between. Reading it meant re-finding the same field at a
/// different height in every card.
///
/// These lay the cards out for real. A card's height is the sum of what its
/// content happens to need, and every "reserve a line" modifier that would
/// hold that constant is invisible in the source — deleting one still
/// compiles, still runs, and quietly brings the ragged strip back.
@MainActor
struct ProviderCapacityCardTests {
    private static let cardWidth: CGFloat = 250
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

    private func height(_ json: String) throws -> CGFloat {
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
        // A compressed proposal, not the offered height: the card ends in a
        // `Spacer` so it will happily accept whatever it is given, and asking
        // for 900pt gets 900pt back from every card alike — a green that
        // measures the proposal rather than the card.
        return host.sizeThatFits(
            in: CGSize(width: Self.cardWidth, height: UIView.layoutFittingCompressedSize.height)
        ).height
    }

    // MARK: - Tests

    /// The whole point. Four windows, one window and no windows all render the
    /// same card, so a field sits at the same height in every card in the strip.
    @Test func everyHealthyProviderRendersTheSameHeightCard() throws {
        let heights = try [Self.anthropic, Self.openai, Self.google].map(height)

        #expect(heights[0] == heights[1],
                "four-window \(heights[0])pt vs one-window \(heights[1])pt")
        #expect(heights[1] == heights[2],
                "one-window \(heights[1])pt vs no-window \(heights[2])pt")
    }

    /// A failed probe earns exactly one extra line to say why, and no more —
    /// the reason is the one thing on the card that cannot be reserved for,
    /// since healthy cards must not carry a blank line waiting for it.
    @Test func aFailedProbeCostsAtMostOneLine() throws {
        let healthy = try height(Self.google)
        let failed = try height(Self.errored)

        #expect(failed > healthy, "the reason line did not render")
        #expect(failed - healthy <= 30, "reason line added \(failed - healthy)pt")
    }

    /// Collapsing four windows to one is what makes the heights equal, so the
    /// card must stay near a fixed handful of lines rather than growing with
    /// whatever the provider happens to report.
    @Test func theCardDoesNotGrowWithWindowCount() throws {
        #expect(try height(Self.anthropic) <= 200)
    }

    /// The bar is the visual indicator the card exists to show. A subscription
    /// with no usable reading renders the track and no fill, which is a
    /// different statement from a full bar or an empty one.
    @Test func theHeadlineBarFillsToTheTightestWindow() throws {
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
