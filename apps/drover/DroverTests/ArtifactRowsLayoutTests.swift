import SwiftUI
import Testing
import UIKit
@testable import Drover
@testable import DroverKit

/// The pane's one job beyond showing artifacts: not taking the screen.
///
/// These lay it out for real in a hosting controller rather than asserting on
/// a constant. Every way of saying "as tall as the content, up to a cap" that
/// SwiftUI offers behaved differently from its documentation here — a
/// geometry reader driving its own scroll view's frame collapsed to 1pt,
/// `ViewThatFits` took the scrolling fallback for a single row — and none of
/// that is visible without an actual layout pass.
@MainActor
struct ArtifactRowsLayoutTests {
    private static let phoneWidth: CGFloat = 393
    /// A tall phone's transcript area. If the pane grew to fill what it was
    /// offered, this is what it would grow to.
    private static let offeredHeight: CGFloat = 800

    private func artifacts(_ count: Int) -> [SessionArtifact] {
        (0..<count).map { index in
            index.isMultiple(of: 2)
                ? SessionArtifact(kind: .branch, value: "fix/hook-span-and-session-timestamps-\(index)")
                : SessionArtifact(kind: .pullRequest,
                                  value: "arniesaha/agentweave #\(250 + index)",
                                  url: URL(string: "https://github.com/arniesaha/agentweave/pull/\(250 + index)"))
        }
    }

    private func height(of artifacts: [SessionArtifact], expanded: Bool = true) -> CGFloat {
        let pane = ArtifactRows(artifacts: artifacts, initiallyExpanded: expanded)
        let host = UIHostingController(rootView: pane.droverTint())
        host.view.frame = CGRect(x: 0, y: 0, width: Self.phoneWidth, height: Self.offeredHeight)
        host.view.layoutIfNeeded()
        return host.sizeThatFits(in: CGSize(width: Self.phoneWidth,
                                            height: Self.offeredHeight)).height
    }

    /// The pane arrives as one header row. It competes with the transcript
    /// for the same screen, and the header already answers the question it
    /// exists for — how many, and were there any.
    @Test func thePaneStartsCollapsed() {
        for count in [1, 3, 9, 40] {
            #expect(height(of: artifacts(count), expanded: false) <= 60,
                    "\(count) artifacts opened to more than a header row")
        }
    }

    /// Collapsed is a default, not a ceiling: opening it has to actually
    /// show the rows.
    @Test func openingThePaneShowsTheRows() {
        #expect(height(of: artifacts(3), expanded: true)
                > height(of: artifacts(3), expanded: false))
    }

    /// The screenshot that started this: nine artifacts filled the phone and
    /// left the transcript a strip. Opened, however many there are, the pane
    /// stays a pane.
    @Test func manyArtifactsStayBounded() {
        #expect(height(of: artifacts(9)) <= 230)
        #expect(height(of: artifacts(40)) <= 230)
    }

    /// The other half of the contract: the cap is a cap, not a fixed size.
    /// Two artifacts must not be given the same slab as forty.
    @Test func fewArtifactsDoNotReserveTheCap() {
        let small = height(of: artifacts(2))
        let large = height(of: artifacts(9))

        #expect(small < large)
        #expect(small > 0)
    }

    /// A single artifact is still a readable row, not a collapsed sliver.
    @Test func oneArtifactRendersItsRow() {
        #expect(height(of: artifacts(1)) > 60)
    }

    /// Bounded must mean scrollable, not clipped: the rows past the cap have
    /// to still be laid out and reachable, or capping the pane would just
    /// hide the pull request you opened two minutes ago.
    @Test func theOverflowScrollsRatherThanDisappearing() {
        let host = hosted(artifacts(9))

        guard let scrollView = firstScrollView(in: host.view) else {
            Issue.record("the capped pane is not backed by a scroll view")
            return
        }
        #expect(scrollView.contentSize.height > scrollView.bounds.height,
                "content is \(scrollView.contentSize.height) in a \(scrollView.bounds.height) frame")
    }

    /// Three rows fit whole, so there is nothing to scroll and no scroll view
    /// to bounce — the pane is a plain stack at its natural height.
    @Test func aShortListIsNotAScrollView() {
        let host = hosted(artifacts(3))

        #expect(firstScrollView(in: host.view) == nil)
    }

    /// A scene-attached window: SwiftUI only materialises the UIKit views
    /// behind a `ScrollView` once the hierarchy is in a real window.
    private func hosted(_ artifacts: [SessionArtifact]) -> UIHostingController<some View> {
        let pane = ArtifactRows(artifacts: artifacts, initiallyExpanded: true)
        let host = UIHostingController(rootView: pane.droverTint())
        let scene = UIApplication.shared.connectedScenes.first as? UIWindowScene
        let window = scene.map { UIWindow(windowScene: $0) }
            ?? UIWindow(frame: CGRect(x: 0, y: 0, width: Self.phoneWidth, height: Self.offeredHeight))
        window.frame = CGRect(x: 0, y: 0, width: Self.phoneWidth, height: Self.offeredHeight)
        window.rootViewController = host
        window.makeKeyAndVisible()
        window.layoutIfNeeded()
        host.view.layoutIfNeeded()
        return host
    }

    private func firstScrollView(in view: UIView) -> UIScrollView? {
        if let scrollView = view as? UIScrollView { return scrollView }
        for subview in view.subviews {
            if let found = firstScrollView(in: subview) { return found }
        }
        return nil
    }
}
