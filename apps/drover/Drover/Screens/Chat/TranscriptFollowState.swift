import SwiftUI

/// Layout changes cannot change following. Only a user scroll or jump can.
struct TranscriptFollowState {
    private(set) var isFollowing = true
    private(set) var newEventCount = 0
    private(set) var hasUnseenContent = false
    private var anchor: (id: String, viewportY: CGFloat)?

    mutating func positionChanged(bottomDistance: CGFloat, isUserDriven: Bool) {
        guard isUserDriven else { return }
        if bottomDistance <= 48 { jumpToLatest() } else { detach() }
    }

    mutating func detach() { isFollowing = false }

    /// The return value authorizes a scroll to the tail.
    mutating func contentChanged() -> Bool {
        if !isFollowing { hasUnseenContent = true }
        return isFollowing
    }

    mutating func recordNewEvents(_ count: Int) {
        guard !isFollowing else { return }
        newEventCount += max(0, count)
        hasUnseenContent = true
    }

    mutating func jumpToLatest() {
        isFollowing = true
        newEventCount = 0
        hasUnseenContent = false
        anchor = nil
    }

    mutating func captureAnchor(in frames: [String: CGRect], offset: CGFloat) {
        guard !isFollowing else { return }
        // Preserve the partial row offset. Growth inside it leaves the reader
        // at the same distance from its start; growth above moves its origin.
        guard let row = frames.filter({ $0.value.maxY > offset })
            .min(by: { $0.value.minY < $1.value.minY }) else { return }
        anchor = (row.key, row.value.minY - offset)
    }

    func preservedOffset(in frames: [String: CGRect]) -> CGFloat? {
        guard !isFollowing, let anchor, let frame = frames[anchor.id] else { return nil }
        return max(0, frame.minY - anchor.viewportY)
    }
}

struct TranscriptScrollGeometry: Equatable {
    let offset: CGFloat
    let bottomDistance: CGFloat
    let contentHeight: CGFloat
    let viewportHeight: CGFloat
}

struct TranscriptRowFrames: PreferenceKey {
    static let defaultValue: [String: CGRect] = [:]

    static func reduce(value: inout [String: CGRect], nextValue: () -> [String: CGRect]) {
        value.merge(nextValue(), uniquingKeysWith: { _, new in new })
    }
}
