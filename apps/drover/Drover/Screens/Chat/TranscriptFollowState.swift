import SwiftUI

/// Layout changes cannot change following. Only a user scroll or jump can.
struct TranscriptFollowState {
    private(set) var isFollowing = true
    private(set) var newEventCount = 0
    private(set) var hasUnseenContent = false
    private var followingLayoutGrowth: CGFloat = 0
    private var anchor: (id: String, viewportY: CGFloat)?

    mutating func positionChanged(bottomDistance: CGFloat, isUserDriven: Bool,
                                 isDecelerating: Bool = false) {
        guard isUserDriven else { return }
        if isFollowing {
            // A direct drag can reach the new end and reverse direction. A
            // decelerating bounce can still return to its old target after growth.
            if bottomDistance <= 48 && !isDecelerating { followingLayoutGrowth = 0 }
            if bottomDistance - followingLayoutGrowth > 48 { detach() }
        } else if bottomDistance <= 48 {
            jumpToLatest()
        }
    }

    /// Content arriving during deceleration must not look like scrolling away.
    /// Keep the threshold relative to the end the user actually reached until idle.
    mutating func layoutChanged(bottomDistanceChange: CGFloat) {
        guard isFollowing else { return }
        followingLayoutGrowth = max(0, followingLayoutGrowth + bottomDistanceChange)
    }

    mutating func scrollingBegan() { followingLayoutGrowth = 0 }

    mutating func scrollingEnded(bottomDistance: CGFloat) {
        // Movement already detaches during the gesture. An append or resize
        // after reaching bottom must not undo the user's return to following.
        followingLayoutGrowth = 0
        guard !isFollowing else { return }
        positionChanged(bottomDistance: bottomDistance, isUserDriven: true)
    }

    mutating func detach() {
        isFollowing = false
        followingLayoutGrowth = 0
    }

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
        followingLayoutGrowth = 0
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
    let bottomInset: CGFloat

    init(contentOffset: CGFloat, contentHeight: CGFloat, viewportHeight: CGFloat,
         topInset: CGFloat, bottomInset: CGFloat) {
        offset = contentOffset + topInset
        // ScrollGeometry's viewport already excludes the composer/safe-area
        // insets. Measure in the same content coordinates as ScrollPosition;
        // adding insets again leaves a false gap at the physical bottom.
        // Overscroll and content shorter than the viewport are both at bottom.
        bottomDistance = max(0, contentHeight - offset - viewportHeight)
        self.contentHeight = contentHeight
        self.viewportHeight = viewportHeight
        self.bottomInset = bottomInset
    }

    init(_ geometry: ScrollGeometry) {
        self.init(contentOffset: geometry.contentOffset.y,
                  contentHeight: geometry.contentSize.height,
                  viewportHeight: geometry.containerSize.height,
                  topInset: geometry.contentInsets.top,
                  bottomInset: geometry.contentInsets.bottom)
    }
}

struct TranscriptRowFrames: PreferenceKey {
    static let defaultValue: [String: CGRect] = [:]

    static func reduce(value: inout [String: CGRect], nextValue: () -> [String: CGRect]) {
        value.merge(nextValue(), uniquingKeysWith: { _, new in new })
    }
}
