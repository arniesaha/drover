import Foundation
import SwiftUI
import Testing
import UIKit
@testable import Drover
@testable import DroverKit

/// The redesign's measurable promises: nothing clips, everything wraps, and
/// every row stays tappable — including at accessibility text sizes, which is
/// exactly where the shipped screen fell apart (a three-line caption became
/// five, and five rows filled the screen).
@MainActor
struct DistributionLayoutTests {
    private static let phoneWidth: CGFloat = 365  // 393pt phone less the insets

    private static func row(
        title: String = "openclaw",
        detail: String = "23,832,216 tokens",
        secondary: String? = nil,
        unreported: Bool = false,
        age: String? = nil
    ) -> DistributionRowPresentation {
        DistributionRowPresentation(
            id: title,
            title: title,
            valueText: "14",
            shareFraction: 0.6,
            detailText: detail,
            secondaryText: secondary,
            tokensUnreported: unreported,
            ageText: age,
            isStale: age != nil,
            accessibilityLabel: "\(title), 14 sessions, \(detail)"
        )
    }

    private static func height(
        _ presentation: DistributionRowPresentation,
        typeSize: DynamicTypeSize = .large,
        width: CGFloat = phoneWidth
    ) -> CGFloat {
        let view = DistributionRow(row: presentation, glyph: "cpu")
            .frame(width: width)
            .dynamicTypeSize(typeSize)
            .droverTint()
        let host = UIHostingController(rootView: view)
        host.view.frame = CGRect(x: 0, y: 0, width: width, height: 600)
        host.view.layoutIfNeeded()
        // Deliberately an unbounded height proposal. `layoutFittingCompressedSize`
        // is zero, and against a view carrying `.frame(minHeight: 44)` that
        // returns the 44pt floor for every input — which silently turns every
        // height assertion below into a tautology.
        return host.sizeThatFits(
            in: CGSize(width: width, height: .greatestFiniteMagnitude)
        ).height
    }

    // MARK: - Row is always tappable

    @Test func everyRowClearsTheMinimumTapTarget() {
        // The design commits to 44pt, and the shortest possible row — short
        // name, no age, no secondary line — is the one that would miss it.
        #expect(Self.height(Self.row(title: "a", detail: "0 tokens")) >= 44)
    }

    @Test func theShortestRowStillClearsItAtLargeType() {
        #expect(
            Self.height(Self.row(title: "a", detail: "0 tokens"), typeSize: .accessibility3) >= 44
        )
    }

    // MARK: - Nothing clips

    @Test func aLongNameGrowsTheRowInsteadOfBeingCut() {
        let short = Self.height(Self.row(title: "agy"))
        let long = Self.height(
            Self.row(title: "claude-sonnet-4-5-20260514[1m-thinking-xhigh]")
        )

        // Wrapping is the whole point: a taller row is correct, a clipped one
        // is not.
        #expect(long > short, "long name did not wrap (both \(short)pt)")
    }

    @Test func accessibilityTypeGrowsTheRowRatherThanTruncating() {
        let normal = Self.height(Self.row())
        let large = Self.height(Self.row(), typeSize: .accessibility3)

        #expect(large > normal, "row did not grow at accessibility3")
    }

    @Test func aNarrowScreenStillLaysTheRowOut() {
        // The smallest phone Drover targets, less insets.
        let narrow = Self.height(Self.row(), width: 292)

        #expect(narrow >= 44)
    }

    // MARK: - The collapse actually collapsed

    @Test func aRowWithoutASecondaryLineIsShorterThanTheShippedThreeLineRow() {
        // The shipped row carried name + figures + a three-part caption that
        // wrapped to two lines. One line plus a bar has to be materially
        // shorter or none of this was worth doing.
        let collapsed = Self.height(Self.row())
        let withEverything = Self.height(
            Self.row(
                secondary: "claude-code, codex · mac-mini, nas",
                age: "4d"
            )
        )

        #expect(collapsed < withEverything)
        #expect(collapsed <= 80, "a one-line row is \(collapsed)pt")
    }

    @Test func aRowThatMatchesItsSectionIsShorterThanOneThatDiffers() {
        // "Say it once" only pays off if the quiet row is actually smaller.
        #expect(Self.height(Self.row()) <= Self.height(Self.row(age: "4d")))
    }

    // MARK: - Unreported

    // MARK: - Heading

    private static func headingHeight(typeSize: DynamicTypeSize) -> CGFloat {
        let section = DistributionSectionPresentation(
            title: "Harnesses",
            subtitle: "5 harnesses · ranked by sessions",
            rank: .sessions,
            rows: []
        )
        let view = DistributionSectionView(section: section, glyph: "cpu", onToggleRank: {})
            .frame(width: phoneWidth)
            .dynamicTypeSize(typeSize)
            .droverTint()
        let host = UIHostingController(rootView: view)
        host.view.frame = CGRect(x: 0, y: 0, width: phoneWidth, height: 600)
        host.view.layoutIfNeeded()
        return host.sizeThatFits(
            in: CGSize(width: phoneWidth, height: .greatestFiniteMagnitude)
        ).height
    }

    @Test func theHeadingWrapsTheToggleInsteadOfSqueezingTheTitle() {
        // Sharing one line at accessibility sizes hyphenated the title into
        // "HARNESS-ES". Wrapping costs height, which is the tell.
        let normal = Self.headingHeight(typeSize: .large)
        let large = Self.headingHeight(typeSize: .accessibility3)

        #expect(large > normal, "heading did not wrap at accessibility3")
    }

    @Test func anUnreportedRowIsTheSameHeightAsAReportedOne() {
        // Unreported changes the bar and the wording, not the layout — a list
        // where half the rows report and half do not must not look ragged.
        let reported = Self.height(Self.row(detail: "23,832,216 tokens"))
        let unreported = Self.height(
            Self.row(detail: "tokens not reported", unreported: true)
        )

        #expect(reported == unreported)
    }
}

/// The filter strip has to show every filter at once. Parked off the right
/// edge of a horizontal scroller, the later ones may as well not exist.
@MainActor
struct FlowLayoutTests {
    private static func size(width: CGFloat, count: Int, typeSize: DynamicTypeSize = .large)
        -> CGSize {
        let view = FlowLayout(spacing: 8, lineSpacing: 8) {
            ForEach(0..<count, id: \.self) { index in
                Text("Filter \(index)")
                    .padding(.horizontal, 12)
                    .frame(height: 32)
            }
        }
        .dynamicTypeSize(typeSize)

        // No `.frame(width:)`: fixing the width would force the reported size
        // to that width and hide whether the layout wrapped or overflowed.
        let host = UIHostingController(rootView: view)
        host.view.frame = CGRect(x: 0, y: 0, width: width, height: 600)
        host.view.layoutIfNeeded()
        return host.sizeThatFits(
            in: CGSize(width: width, height: .greatestFiniteMagnitude)
        )
    }

    @Test func chipsWrapInsteadOfOverflowing() {
        let size = Self.size(width: 365, count: 6)

        // Six chips cannot fit on one 365pt line, so the layout must be
        // taller than a single row rather than wider than the screen.
        #expect(size.width <= 365, "flow ran \(size.width)pt wide on a 365pt screen")
        #expect(size.height > 40, "six chips collapsed onto one line")
    }

    @Test func moreChipsMeansMoreHeightNotMoreWidth() {
        let few = Self.size(width: 365, count: 2)
        let many = Self.size(width: 365, count: 8)

        #expect(many.height > few.height)
        #expect(many.width <= 365)
    }

    @Test func aSingleChipDoesNotClaimTheWholeWidth() {
        // sizeThatFits reports what it used, so a trailing control beside the
        // strip is not pushed off the edge.
        #expect(Self.size(width: 365, count: 1).width < 365)
    }

    @Test func accessibilityTypeWrapsFurtherRatherThanClipping() {
        let normal = Self.size(width: 365, count: 6)
        let large = Self.size(width: 365, count: 6, typeSize: .accessibility3)

        #expect(large.height > normal.height)
        #expect(large.width <= 365)
    }

    @Test func aChipWiderThanTheLineStillGetsALine() {
        // A single over-wide child must not be dropped or clipped to nothing.
        let view = FlowLayout {
            Text(String(repeating: "wide", count: 40)).fixedSize()
        }
        .frame(width: 200)
        let host = UIHostingController(rootView: view)
        host.view.frame = CGRect(x: 0, y: 0, width: 200, height: 400)
        host.view.layoutIfNeeded()

        #expect(
            host.sizeThatFits(
                in: CGSize(width: 200, height: UIView.layoutFittingCompressedSize.height)
            ).height > 0
        )
    }
}
