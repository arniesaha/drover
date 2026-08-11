import Foundation
import SwiftUI
import Testing
import UIKit
@testable import Drover
@testable import DroverKit

/// The cost of pinning something is the screen it stops giving back.
///
/// The capacity strip used to scroll away with everything else, so its height
/// was nobody's problem. Pinned above the list (#80) it is charged to every
/// frame the inbox ever draws, and the list gets what is left. These lay the
/// header out for real and hold it to a budget — a second row of cards, an
/// un-clamped account label or a stray section wired into the header would all
/// compile, run, and quietly eat the list.
@MainActor
struct InboxStatusHeaderLayoutTests {
    /// iPhone 15/16 portrait width, and a screen tall enough that nothing here
    /// is being squeezed by the proposal.
    private static let phoneWidth: CGFloat = 393
    private static let phoneHeight: CGFloat = 852

    /// Four windows and a two-line account label — the tallest card the strip
    /// renders (see `ProviderCapacityCardTests`).
    private static let anthropic = """
    {"snapshot_id":"s1","dedup_key":"k1","provider":"anthropic",
     "account_label":"arnab.saha@atlan.com","plan_label":"team",
     "host_id":"work-laptop","status":"ok","observed_at":"2026-08-09T18:00:00Z",
     "source":"claude-oauth-usage",
     "windows":[{"kind":"extra_usage","used_percent":3.6},
                {"kind":"five_hour","used_percent":4,"resets_at":"2026-08-09T20:00:00Z"},
                {"kind":"seven_day","used_percent":26,"resets_at":"2026-08-10T08:00:00Z"}]}
    """

    private static let openai = """
    {"snapshot_id":"s2","dedup_key":"k2","provider":"openai",
     "account_label":"me@example.com","plan_label":"prolite",
     "host_id":"mac-mini","status":"ok","observed_at":"2026-08-09T18:00:00Z",
     "source":"codex-app-server",
     "windows":[{"kind":"primary","used_percent":71,"resets_at":"2026-08-14T18:00:00Z"}]}
    """

    private func accounts(_ json: [String]) throws -> [ProviderAccount] {
        try json.map { try JSONDecoder().decode(ProviderAccount.self, from: Data($0.utf8)) }
    }

    private func height(accounts: [ProviderAccount], hostCount: Int = 3) throws -> CGFloat {
        let snapshot = try HarnessSnapshot.decode(from: fleetSnapshotJSON)
        let groups = Array(SessionStore.hostGroups(
            hosts: snapshot.hosts,
            sessions: snapshot.sessions
        ).prefix(hostCount))

        let header = InboxStatusHeader(
            summary: FleetSummaryPresentation(snapshot: snapshot),
            hostGroups: groups,
            onRetry: {}
        ) {
            if !accounts.isEmpty {
                ProviderCapacitySection(
                    accounts: accounts,
                    status: .ok,
                    statusMessage: nil,
                    hostTitles: ["work-laptop": "work-laptop", "mac-mini": "Mac Mini", "nas": "NAS"],
                    onOpenAnalytics: {}
                )
            }
        }

        let host = UIHostingController(rootView: header.droverTint())
        host.view.frame = CGRect(x: 0, y: 0, width: Self.phoneWidth, height: Self.phoneHeight)
        host.view.layoutIfNeeded()
        return host.sizeThatFits(
            in: CGSize(width: Self.phoneWidth, height: UIView.layoutFittingCompressedSize.height)
        ).height
    }

    /// The budget. Chrome row and the "New Session" bar take their own bites of
    /// an 852pt phone; the header has to leave the list the majority of what is
    /// left, which means staying under about 40% of the screen. It measures
    /// 322pt today — fleet line, host strip and one row of cards.
    @Test func thePinnedHeaderLeavesTheListMostOfTheScreen() throws {
        let measured = try height(accounts: accounts([Self.anthropic, Self.openai]))

        #expect(measured <= Self.phoneHeight * 0.4,
                "pinned header is \(measured)pt of a \(Self.phoneHeight)pt screen")
    }

    /// The strip scrolls sideways rather than wrapping, so the fifth
    /// subscription costs the list nothing. This is the property that makes
    /// pinning affordable at all.
    @Test func moreSubscriptionsDoNotMakeTheHeaderTaller() throws {
        let two = try height(accounts: accounts([Self.anthropic, Self.openai]))
        let many = try height(accounts: accounts(
            [Self.anthropic, Self.openai, Self.anthropic, Self.openai, Self.anthropic]
        ))

        #expect(two == many, "two cards \(two)pt vs five cards \(many)pt")
    }

    /// A hub with no cockpit gets no empty slab: with nothing to report the
    /// header falls back to the fleet line and the host strip alone.
    @Test func aHubWithoutCapacityKeepsTheHeaderSmall() throws {
        let withCapacity = try height(accounts: accounts([Self.anthropic]))
        let without = try height(accounts: [])

        #expect(without < withCapacity)
        #expect(without <= 120, "bare header is \(without)pt")
    }
}
