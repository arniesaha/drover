import Foundation
import SwiftUI
import Testing
import UIKit
@testable import Drover
@testable import DroverKit

/// The cockpit's grids and headings have to reflow, not squeeze.
///
/// `GridItem.adaptive(minimum:)` measures against a fixed point width, so it
/// has no idea the text grew: at accessibility sizes it kept packing three
/// metric columns and two project cards, which hyphenated "BUSIEST PROJECT-S",
/// stacked "Ob-served ses-sions" into syllables, and split the abbreviated
/// token count into "63.1" over "M". Every test here fails if the minimums
/// stop scaling.
@MainActor
struct CockpitReflowTests {
    private static let phoneWidth: CGFloat = 365

    private static func decode<T: Decodable>(_ type: T.Type, _ json: String) -> T {
        // swiftlint:disable:next force_try
        try! JSONDecoder().decode(type, from: Data(json.utf8))
    }

    private static let pageJSON = #"{"limit": 25, "next_cursor": null}"#

    private static func activity(
        tokens: Int = 63_132_964, costUSD: Double = 0.589, costPercent: Double? = 5.0
    ) -> ActivitySummary {
        let cost = costPercent.map { "\($0)" } ?? "null"
        return decode(ActivitySummary.self, """
        {
          "totals": {"session_count": 240, "total_tokens": \(tokens), "cost_usd": \(costUSD),
                     "cache_read_tokens": 0, "cache_write_tokens": 0, "total_latency_ms": 0},
          "projects": [], "harnesses": [], "hosts": [], "models": [],
          "project_metric": "sessions",
          "coverage": {"source": "drover_observed", "token_percent": 5.8333,
                       "cost_percent": \(cost)},
          "pagination": {"projects": \(pageJSON), "harnesses": \(pageJSON),
                         "hosts": \(pageJSON), "models": \(pageJSON)}
        }
        """)
    }

    private static func projects(_ count: Int) -> [PopularProject] {
        let names = ["arniesaha/drover", "arniesaha/openclaw", "arniesaha/agentweave"]
        let items = (0..<count).map { index in
            """
            {"project_key": "\(names[index % names.count])", "session_count": \(61 - index * 10),
             "total_tokens": 0, "cost_usd": 0, "cache_read_tokens": 0, "cache_write_tokens": 0,
             "total_latency_ms": 0, "harnesses": ["claude-code"], "hosts": ["mac-mini"],
             "metric": "sessions"}
            """
        }.joined(separator: ",")
        return decode([PopularProject].self, "[\(items)]")
    }

    private static func height<V: View>(_ view: V, typeSize: DynamicTypeSize) -> CGFloat {
        let hosted = view
            .frame(width: phoneWidth)
            .dynamicTypeSize(typeSize)
            .droverTint()
        let host = UIHostingController(rootView: hosted)
        host.view.frame = CGRect(x: 0, y: 0, width: phoneWidth, height: 1200)
        host.view.layoutIfNeeded()
        // Unbounded height: a compressed proposal returns any minHeight floor
        // instead of the content, which makes these assertions tautologies.
        return host.sizeThatFits(
            in: CGSize(width: phoneWidth, height: .greatestFiniteMagnitude)
        ).height
    }

    // MARK: - The metric triad

    @Test func theMetricTriadReflowsAtAccessibilitySizes() {
        let section = ActivitySummarySection(
            activity: Self.activity(), statusMessage: nil, onOpenAnalytics: {}
        )

        let normal = Self.height(section, typeSize: .large)
        let large = Self.height(section, typeSize: .accessibility3)

        // Three columns becoming one is a large height change, not a nudge.
        #expect(large > normal * 1.5, "triad did not reflow: \(normal)pt → \(large)pt")
    }

    @Test func aHugeTokenCountDoesNotGrowTheCard() {
        // Abbreviation is what keeps the row a row. If the raw figure came
        // back, the card would grow to fit nine digits.
        let small = Self.height(
            ActivitySummarySection(
                activity: Self.activity(tokens: 240), statusMessage: nil, onOpenAnalytics: {}
            ),
            typeSize: .large
        )
        let huge = Self.height(
            ActivitySummarySection(
                activity: Self.activity(tokens: 63_132_964), statusMessage: nil,
                onOpenAnalytics: {}
            ),
            typeSize: .large
        )

        #expect(small == huge)
    }

    @Test func costCoverageIsOnlyPrintedWhenItDiffersFromTokenCoverage() {
        // Saying "5.8% token coverage · 5.8% cost coverage" is noise; the
        // second half only earns its line when the two disagree.
        let differing = Self.height(
            ActivitySummarySection(
                activity: Self.activity(costPercent: 5.0), statusMessage: nil,
                onOpenAnalytics: {}
            ),
            typeSize: .large
        )
        let same = Self.height(
            ActivitySummarySection(
                activity: Self.activity(costPercent: 5.8333), statusMessage: nil,
                onOpenAnalytics: {}
            ),
            typeSize: .large
        )

        #expect(same <= differing)
    }

    @Test func theUnreportedCostWordingDoesNotGrowTheCard() {
        // "Not reported" is twice the width of "$0.59" and lands in the same
        // one-line slot the abbreviated token count is protected by. If it
        // wrapped, a subscription-billed fleet — the ordinary case for this
        // metric, see #150 — would see a permanently taller card.
        let reported = Self.height(
            ActivitySummarySection(
                activity: Self.activity(), statusMessage: nil, onOpenAnalytics: {}
            ),
            typeSize: .large
        )
        let unreported = Self.height(
            ActivitySummarySection(
                activity: Self.activity(costUSD: 0, costPercent: 0), statusMessage: nil,
                onOpenAnalytics: {}
            ),
            typeSize: .large
        )

        #expect(reported == unreported)
    }

    // MARK: - Project cards

    @Test func projectCardsGoOneUpAtAccessibilitySizes() {
        let section = PopularProjectsSection(
            projects: Self.projects(2), tokenCoveragePercent: 5.8333, onOpenAnalytics: {}
        )

        let normal = Self.height(section, typeSize: .large)
        let large = Self.height(section, typeSize: .accessibility3)

        // Two side-by-side becoming two stacked roughly doubles the block.
        #expect(large > normal * 1.5, "cards did not reflow: \(normal)pt → \(large)pt")
    }

    @Test func moreProjectsMeansMoreHeightNotAWiderScroller() {
        // The old version was a horizontal scroller, so a third project was
        // invisible off the right edge rather than on a second row.
        let two = Self.height(
            PopularProjectsSection(
                projects: Self.projects(2), tokenCoveragePercent: nil, onOpenAnalytics: {}
            ),
            typeSize: .large
        )
        let four = Self.height(
            PopularProjectsSection(
                projects: Self.projects(4), tokenCoveragePercent: nil, onOpenAnalytics: {}
            ),
            typeSize: .large
        )

        #expect(four > two)
    }

    @Test func anEmptySectionDoesNotClaimSpaceForASubtitle() {
        let empty = Self.height(
            PopularProjectsSection(
                projects: [], tokenCoveragePercent: 5.8333, onOpenAnalytics: {}
            ),
            typeSize: .large
        )
        let populated = Self.height(
            PopularProjectsSection(
                projects: Self.projects(1), tokenCoveragePercent: 5.8333, onOpenAnalytics: {}
            ),
            typeSize: .large
        )

        #expect(empty < populated)
    }

    // MARK: - Section heading

    @Test func theSectionHeadingWrapsRatherThanHyphenating() {
        let heading = CockpitSectionHeading(
            title: "Busiest projects", source: "Drover observed", action: {}
        )

        let normal = Self.height(heading, typeSize: .large)
        let large = Self.height(heading, typeSize: .accessibility3)

        // Sharing one line is what produced "BUSIEST PROJECT-S"; wrapping
        // costs height, which is the observable tell.
        #expect(large > normal, "heading did not wrap at accessibility3")
    }
}
