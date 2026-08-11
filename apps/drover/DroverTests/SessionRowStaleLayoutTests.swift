import Foundation
import SwiftUI
import Testing
import UIKit
@testable import Drover
@testable import DroverKit

/// A stale card has to look different without moving.
///
/// Marking staleness is only useful if the list does not reflow when a link
/// drops: a hub that flaps in and out would otherwise make every row below the
/// first stale one jump, which is its own kind of "the app is wrong". The
/// stale note deliberately sits in the slot the verb vacated, at the same
/// padding — a property that is invisible in the source and that a future
/// edit (a second line, an icon, a capsule) would quietly break.
@MainActor
struct SessionRowStaleLayoutTests {
    private static let phoneWidth: CGFloat = 365  // 393pt phone less the list's insets

    private static let snapshotTaken = Date(timeIntervalSince1970: 1_754_913_600)

    /// The reported session (#81): a question asked 27 minutes before the last
    /// snapshot landed.
    private static let openclaw = SessionSummary(
        id: "openclaw", hostID: "nas", harness: "claude-code", mode: "structured",
        status: "running", awaiting: "input", cwd: "/home/arnab/src/openclaw",
        lastActivity: snapshotTaken.addingTimeInterval(-27 * 60),
        preview: "Yeah go ahead"
    )

    private func height(stale: Bool, session: SessionSummary = openclaw) -> CGFloat {
        let row = SessionRow(
            session: session,
            hostTitle: "NAS",
            freshness: SnapshotFreshness(
                lastUpdate: Self.snapshotTaken,
                isReachable: !stale,
                now: Self.snapshotTaken.addingTimeInterval(stale ? 600 : 1)
            )
        )
        let host = UIHostingController(rootView: row.frame(width: Self.phoneWidth).droverTint())
        host.view.frame = CGRect(x: 0, y: 0, width: Self.phoneWidth, height: 400)
        host.view.layoutIfNeeded()
        return host.sizeThatFits(
            in: CGSize(width: Self.phoneWidth, height: UIView.layoutFittingCompressedSize.height)
        ).height
    }

    @Test func goingStaleDoesNotResizeTheCard() {
        let live = height(stale: false)
        let stale = height(stale: true)

        #expect(live == stale, "card moved from \(live)pt to \(stale)pt when the hub went away")
    }

    /// The same holds for a card with no preview, where the state phrase is
    /// promoted into the title — the tallest thing the stale treatment touches.
    @Test func goingStaleDoesNotResizeAPlaceholderCard() {
        let placeholder = SessionSummary(
            id: "s", hostID: "nas", harness: "claude-code", mode: "structured",
            status: "running", awaiting: "approval",
            cwd: "/home/arnab/src/openclaw",
            lastActivity: Self.snapshotTaken.addingTimeInterval(-27 * 60),
            preview: nil
        )

        #expect(height(stale: false, session: placeholder)
                == height(stale: true, session: placeholder))
    }

    /// A row is one card, not a section — the stale treatment must not have
    /// grown it into one.
    @Test func aStaleRowIsStillOneCard() {
        #expect(height(stale: true) <= 110, "stale row is \(height(stale: true))pt")
    }
}
