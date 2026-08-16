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


@Test func fleetSummaryUsesTailscalePresentationOnlyForATransportFailure() throws {
    let snapshot = try HarnessSnapshot.decode(from: fleetSnapshotJSON)

    let transportFailure = FleetSummaryPresentation(
        snapshot: snapshot,
        isReachable: false,
        error: "Can't reach the hub over Tailscale",
        isTailscaleTransportFailure: true
    )
    #expect(transportFailure.isStale == true)
    #expect(transportFailure.fleetLine == "Can't reach the hub over Tailscale")
    #expect(transportFailure.needsCount == 1)

    // A Tailscale address does not make another failure a transport failure.
    let unauthorized = FleetSummaryPresentation(
        snapshot: snapshot,
        isReachable: false,
        error: "Token rejected — check Settings",
        isTailscaleTransportFailure: false
    )
    #expect(unauthorized.fleetLine == "Token rejected — check Settings")

    let malformedResponse = FleetSummaryPresentation(
        snapshot: snapshot,
        isReachable: false,
        error: "Unexpected response from the hub",
        isTailscaleTransportFailure: false
    )
    #expect(malformedResponse.fleetLine == "Unexpected response from the hub")

    let httpFailure = FleetSummaryPresentation(
        snapshot: snapshot,
        isReachable: false,
        error: "Hub is restarting",
        isTailscaleTransportFailure: false
    )
    #expect(httpFailure.fleetLine == "Hub is restarting")

    let repeatedCancellation = FleetSummaryPresentation(
        snapshot: snapshot,
        isReachable: false,
        error: "The first load kept being interrupted before it landed — the hub may be busy.",
        isTailscaleTransportFailure: false
    )
    #expect(repeatedCancellation.fleetLine
            == "The first load kept being interrupted before it landed — the hub may be busy.")

    let success = FleetSummaryPresentation(
        snapshot: snapshot,
        isReachable: true,
        isTailscaleTransportFailure: false
    )
    #expect(success.isStale == false)
    #expect(success.fleetLine == "2 running · 1 finished · 1 host live")
}
