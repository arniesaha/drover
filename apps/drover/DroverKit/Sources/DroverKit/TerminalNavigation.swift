public enum TerminalNavigationDirection: Sendable, Equatable, Hashable {
    case up
    case down
    case left
    case right
}

public enum TerminalNavigationGear: Sendable, Equatable {
    case slow
    case medium
    case fast

    public var repeatInterval: Double {
        switch self {
        case .slow: 0.18
        case .medium: 0.09
        case .fast: 0.04
        }
    }
}

public struct TerminalNavigationMotion: Sendable, Equatable {
    public let direction: TerminalNavigationDirection
    public let gear: TerminalNavigationGear

    public var repeatInterval: Double { gear.repeatInterval }
}

/// Pure state for the terminal's long-press directional gesture.
/// UIKit owns gesture recognition and timers; this type owns the decisions
/// that must remain deterministic and independently testable.
public struct TerminalNavigationRepeater: Sendable {
    public private(set) var motion: TerminalNavigationMotion?

    public init() {}

    /// Returns a direction when the gesture enters a direction or speed gear,
    /// so the UI can send one key immediately before scheduling repeats.
    public mutating func update(
        horizontal: Double,
        vertical: Double
    ) -> TerminalNavigationDirection? {
        let next = Self.motion(horizontal: horizontal, vertical: vertical)
        defer { motion = next }
        guard next != motion else { return nil }
        return next?.direction
    }

    public func repeatedDirection() -> TerminalNavigationDirection? {
        motion?.direction
    }

    public mutating func stop() {
        motion = nil
    }

    private static func motion(
        horizontal: Double,
        vertical: Double
    ) -> TerminalNavigationMotion? {
        let horizontalDistance = abs(horizontal)
        let verticalDistance = abs(vertical)
        let distance = max(horizontalDistance, verticalDistance)
        guard distance >= 18 else { return nil }

        let direction: TerminalNavigationDirection
        if horizontalDistance >= verticalDistance {
            direction = horizontal < 0 ? .left : .right
        } else {
            direction = vertical < 0 ? .up : .down
        }

        let gear: TerminalNavigationGear
        if distance >= 112 {
            gear = .fast
        } else if distance >= 56 {
            gear = .medium
        } else {
            gear = .slow
        }
        return TerminalNavigationMotion(direction: direction, gear: gear)
    }
}
