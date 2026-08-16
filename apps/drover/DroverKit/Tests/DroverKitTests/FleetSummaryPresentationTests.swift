import Foundation
import Testing
@testable import DroverKit

@Test func fleetSummaryCountsTheFleetFromASnapshot() throws {
    // mac-mini online, nas stale, work-laptop offline; two structured
    // sessions working, one awaiting input, one exited PTY.
    let snapshot = try HarnessSnapshot.decode(from: fleetSnapshotJSON)

    let summary = FleetSummaryPresentation(snapshot: snapshot)

    #expect(summary.needsCount == 1)
    #expect(summary.headline == "1 needs you")
    #expect(summary.fleetLine == "2 running · 1 finished · 1 host live")
    #expect(summary.isStale == false)
}

/// Zero waiting sessions means no headline at all — "0 need you" is a number
/// shouting that nothing happened.
@Test func fleetSummarySuppressesTheHeadlineWhenNothingWaits() throws {
    let snapshot = try HarnessSnapshot.decode(from: Data("""
    {"hosts": [{"host_id": "mac-mini", "status": "online",
                "capabilities": {"display_name": "Mac Mini", "harnesses": []}}],
     "sessions": [{"session_id": "a", "host_id": "mac-mini", "harness": "shell",
                   "mode": "pty", "status": "running", "awaiting": null}],
     "cwd_suggestions": []}
    """.utf8))

    let summary = FleetSummaryPresentation(snapshot: snapshot)

    #expect(summary.needsCount == 0)
    #expect(summary.headline == nil)
    #expect(summary.fleetLine == "1 shell attached · 1 host live")
}

@Test func fleetSummaryPluralizesEachClause() throws {
    let snapshot = try HarnessSnapshot.decode(from: Data("""
    {"hosts": [{"host_id": "a", "status": "online",
                "capabilities": {"display_name": "A", "harnesses": []}},
               {"host_id": "b", "status": "online",
                "capabilities": {"display_name": "B", "harnesses": []}}],
     "sessions": [{"session_id": "s1", "host_id": "a", "harness": "shell",
                   "mode": "pty", "status": "running", "awaiting": null},
                  {"session_id": "s2", "host_id": "a", "harness": "shell",
                   "mode": "pty", "status": "running", "awaiting": null},
                  {"session_id": "s3", "host_id": "b", "harness": "codex",
                   "mode": "structured", "status": "running", "awaiting": "approval"}],
     "cwd_suggestions": []}
    """.utf8))

    let summary = FleetSummaryPresentation(snapshot: snapshot)

    #expect(summary.headline == "1 needs you")
    #expect(summary.fleetLine == "2 shells attached · 2 hosts live")
}

/// The degraded case that replaces the unreachable banner: the fleet line
/// carries the error, and `isStale` tells the host strip to drop every dot to
/// its offline form so stale counts can't read as current.
@Test func fleetSummaryReportsAnUnreachableHubInTheFleetLine() throws {
    let snapshot = try HarnessSnapshot.decode(from: fleetSnapshotJSON)

    let summary = FleetSummaryPresentation(
        snapshot: snapshot,
        isReachable: false,
        error: "Server unreachable"
    )

    #expect(summary.isStale == true)
    #expect(summary.fleetLine == "Server unreachable")
    // The headline keeps its last-known value — the sessions really were
    // waiting; we just can't confirm it right now.
    #expect(summary.needsCount == 1)
}

@Test func fleetSummaryHandlesNoSnapshotAtAll() {
    let summary = FleetSummaryPresentation(snapshot: nil)

    #expect(summary.needsCount == 0)
    #expect(summary.headline == nil)
    #expect(summary.fleetLine == "0 hosts live")
}


@Test func fleetSummaryReportsTailscaleNotConnectedWhenTailscaleUnreachable() throws {
    let snapshot = try HarnessSnapshot.decode(from: fleetSnapshotJSON)

    // Unreachable with default error over Tailscale
    let summary1 = FleetSummaryPresentation(
        snapshot: snapshot,
        isReachable: false,
        isTailscale: true
    )
    #expect(summary1.isStale == true)
    #expect(summary1.fleetLine == "Tailscale not connected")
    #expect(summary1.needsCount == 1)

    // Unreachable with Tailscale transport error string
    let summary2 = FleetSummaryPresentation(
        snapshot: snapshot,
        isReachable: false,
        error: "Can't reach the hub over Tailscale",
        isTailscale: true
    )
    #expect(summary2.isStale == true)
    #expect(summary2.fleetLine == "Tailscale not connected")

    // Unreachable with generic unreachable description on Tailscale
    let summary3 = FleetSummaryPresentation(
        snapshot: snapshot,
        isReachable: false,
        error: "Can't reach the hub",
        isTailscale: true
    )
    #expect(summary3.isStale == true)
    #expect(summary3.fleetLine == "Tailscale not connected")

    // Unreachable with explicit non-transport error (e.g. auth error) preserves error
    let summary4 = FleetSummaryPresentation(
        snapshot: snapshot,
        isReachable: false,
        error: "Token rejected — check Settings",
        isTailscale: true
    )
    #expect(summary4.isStale == true)
    #expect(summary4.fleetLine == "Token rejected — check Settings")

    // Reachable on Tailscale returns normal live summary
    let summary5 = FleetSummaryPresentation(
        snapshot: snapshot,
        isReachable: true,
        isTailscale: true
    )
    #expect(summary5.isStale == false)
    #expect(summary5.fleetLine == "2 running · 1 finished · 1 host live")
}
