import Foundation

/// The two lines at the top of the inbox: how many sessions want you, and one
/// quiet line describing the rest of the fleet.
///
/// This is also where hub-unreachable lives. The old design put that in a
/// banner above the list *and* dimmed the whole screen; here the fleet line
/// simply says the hub is gone and the host strip's dots all fall to their
/// offline form. Same real estate, one less piece of chrome, and the counts
/// can't sit there looking authoritative while they're actually stale.
public struct FleetSummaryPresentation: Sendable, Equatable {
    /// Sessions waiting on the user. The headline number; zero means the
    /// headline is suppressed entirely rather than shouting "0 need you".
    public let needsCount: Int
    public let headline: String?
    /// `1 shell attached · 90 finished · 2 hosts live`, or the hub error.
    public let fleetLine: String
    /// True when the counts are last-known rather than current, so the view
    /// can render the strip in its offline form and offer a retry.
    public let isStale: Bool

    public init(snapshot: HarnessSnapshot?, isReachable: Bool = true, error: String? = nil) {
        let sessions = snapshot?.sessions ?? []
        let hosts = snapshot?.hosts ?? []

        needsCount = sessions.filter {
            $0.attention == .needsApproval || $0.attention == .needsInput
        }.count
        headline = needsCount > 0 ? "\(needsCount) need\(needsCount == 1 ? "s" : "") you" : nil
        isStale = !isReachable

        guard isReachable else {
            // Deliberately not the raw transport error: the fleet line is a
            // one-line status, and NexusError's localized descriptions are
            // already sentence-shaped for the retry affordance beside it.
            fleetLine = error ?? "Can't reach the hub"
            return
        }

        let running = sessions.filter { $0.attention == .working && $0.isStructured }.count
        let attached = sessions.filter { $0.attention == .working && !$0.isStructured }.count
        let finished = sessions.filter { $0.attention == .done || $0.attention == .errored }.count
        let live = hosts.filter { $0.presence == .online }.count

        var parts: [String] = []
        if running > 0 { parts.append("\(running) running") }
        if attached > 0 { parts.append(Self.pluralize(attached, "shell attached", "shells attached")) }
        if finished > 0 { parts.append("\(finished) finished") }
        parts.append(Self.pluralize(live, "host live", "hosts live"))
        fleetLine = parts.joined(separator: " · ")
    }

    private static func pluralize(_ count: Int, _ singular: String, _ plural: String) -> String {
        "\(count) \(count == 1 ? singular : plural)"
    }
}
