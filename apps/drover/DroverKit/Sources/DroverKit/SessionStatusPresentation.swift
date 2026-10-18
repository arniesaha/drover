import Foundation

/// Unified status semantic classification and accessibility labels for sessions and hosts.
///
/// ============================================================================
/// STATUS ICON LEGEND
/// ============================================================================
/// The status indicator encodes execution state with consistent, high-contrast
/// semantics shared between host chips and session cards:
///
/// 1. Needs You (`needsApproval` / `needsInput` / host has waiting sessions):
///    - Shape: Solid disc (filled circle).
///    - Color: Warn / Amber (`DroverColor.warn`).
///    - Urgency: High. Operator intervention is required to proceed.
///    - Accessibility: "Needs your attention" / "Needs approval" / "Needs input".
///
/// 2. Running (`working` / host has active running sessions):
///    - Shape: Solid accent disc with animated breathing pulse.
///    - Color: High-contrast Accent Teal (`DroverColor.accentHi`).
///    - Urgency: Normal. Work is actively proceeding in the background.
///    - Accessibility: "Running".
///
/// 3. Finished (`done` / host is online and idle):
///    - Shape: Solid neutral disc.
///    - Color: Calm Muted Neutral (`DroverColor.muted`).
///    - Urgency: Low. Work completed cleanly; no ongoing execution.
///    - Accessibility: "Finished" (card) / "Online, idle" (host).
///
/// 4. Failed (`errored` / host has errored sessions):
///    - Shape: Solid warning disc or distinct warn glyph.
///    - Color: Warn Amber (`DroverColor.warn`).
///    - Urgency: Moderate. Process terminated abnormally.
///    - Accessibility: "Failed".
///
/// 5. Offline (host presence == `.offline`):
///    - Shape: Hollow line ring.
///    - Color: Line (`DroverColor.line`).
///    - Urgency: None. Host is unreachable or turned off.
///    - Accessibility: "Offline".
///
/// 6. Stale (`isStale == true`):
///    - Shape: Dashed line ring.
///    - Color: Muted Neutral (`DroverColor.muted`).
///    - Urgency: Diagnostic. Data reflects last-known cache from unreachable hub.
///    - Accessibility: "Stale: last reported state".
/// ============================================================================
public enum SessionStatusKind: String, Sendable, Equatable {
    case needsYou
    case running
    case finished
    case failed
    case offline
    case stale

    /// Accessible VoiceOver description of the status.
    public var accessibilityLabel: String {
        switch self {
        case .needsYou:
            return "Needs attention"
        case .running:
            return "Running"
        case .finished:
            return "Finished"
        case .failed:
            return "Failed"
        case .offline:
            return "Offline"
        case .stale:
            return "Stale"
        }
    }
}

public struct SessionStatusPresentation: Sendable, Equatable {
    public let kind: SessionStatusKind
    public let accessibilityLabel: String

    public init(attention: AttentionState, isStale: Bool = false) {
        if isStale {
            kind = .stale
            accessibilityLabel = "Stale: last reported state"
            return
        }
        switch attention {
        case .needsApproval:
            kind = .needsYou
            accessibilityLabel = "Needs attention: waiting for approval"
        case .needsInput:
            kind = .needsYou
            accessibilityLabel = "Needs attention: waiting for input"
        case .working:
            kind = .running
            accessibilityLabel = "Running"
        case .done:
            kind = .finished
            accessibilityLabel = "Finished"
        case .errored:
            kind = .failed
            accessibilityLabel = "Failed"
        }
    }

    public init(host: HostSummary, sessions: [SessionSummary], isStale: Bool = false) {
        if isStale {
            kind = .stale
            accessibilityLabel = "\(host.title), stale: hub unreachable"
            return
        }
        if host.presence == .offline {
            kind = .offline
            accessibilityLabel = "\(host.title), offline"
            return
        }

        let hasNeedsYou = sessions.contains { $0.attention == .needsApproval || $0.attention == .needsInput }
        if hasNeedsYou {
            kind = .needsYou
            accessibilityLabel = "\(host.title), live, needs your attention, \(sessions.count) active"
            return
        }

        let hasRunning = sessions.contains { $0.attention == .working }
        if hasRunning {
            kind = .running
            accessibilityLabel = "\(host.title), live, running, \(sessions.count) active"
            return
        }

        let hasFailed = sessions.contains { $0.attention == .errored }
        if hasFailed {
            kind = .failed
            accessibilityLabel = "\(host.title), live, failed sessions"
            return
        }

        kind = .finished
        accessibilityLabel = "\(host.title), live, idle"
    }
}
