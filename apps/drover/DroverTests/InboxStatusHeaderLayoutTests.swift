import Foundation
import SwiftUI
import Testing
import UIKit
@testable import Drover
@testable import DroverKit

/// Home reserves its vertical space for the fleet. Capacity diagnostics now
/// live only in Accounts, so the pinned header must remain compact regardless
/// of the accounts returned by a real hub.
@MainActor
@Suite(.serialized)
struct InboxStatusHeaderLayoutTests {
    @Test func headerLeavesMostOfTheScreenForSessions() throws {
        let snapshot = try HarnessSnapshot.decode(from: fleetSnapshotJSON)
        let header = InboxStatusHeader(
            summary: FleetSummaryPresentation(snapshot: snapshot),
            hostGroups: SessionStore.hostGroups(hosts: snapshot.hosts, sessions: snapshot.sessions),
            onRetry: {}
        )
        let host = UIHostingController(rootView: header.droverTint())
        host.view.frame = CGRect(x: 0, y: 0, width: 393, height: 852)
        host.view.layoutIfNeeded()
        let height = host.sizeThatFits(
            in: CGSize(width: 393, height: UIView.layoutFittingCompressedSize.height)
        ).height

        #expect(height <= 120, "Home header is \(height)pt and should not contain account capacity")
    }
}
