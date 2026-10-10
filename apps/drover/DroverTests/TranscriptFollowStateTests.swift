import Foundation
import Testing
@testable import Drover

struct TranscriptFollowStateTests {
    @Test func bottomThresholdIncludesComposerAndSafeAreaInsets() {
        for bottomInset: CGFloat in [160, 460] {
            for distance: CGFloat in [0, 48, 49] {
                let geometry = TranscriptScrollGeometry(
                    contentOffset: 2000 - (874 - 116 - bottomInset) - distance - 116,
                    contentHeight: 2000, viewportHeight: 874 - 116 - bottomInset,
                    topInset: 116, bottomInset: bottomInset
                )
                #expect(geometry.bottomDistance == distance)
                #expect(geometry.offset == 2000 - (874 - 116 - bottomInset) - distance)
                var state = TranscriptFollowState()
                state.detach()
                state.recordNewEvents(2)
                state.positionChanged(bottomDistance: geometry.bottomDistance, isUserDriven: true)
                #expect(state.isFollowing == (distance <= 48))
                #expect(state.newEventCount == (distance <= 48 ? 0 : 2))
            }
        }
    }

    @Test func reportedViewportAlreadyExcludesSafeAreaAndComposer() {
        // Captured at the physical bottom after keyboard dismissal. The
        // viewport is already reduced by safe areas and the composer.
        let geometry = TranscriptScrollGeometry(
            contentOffset: 6574, contentHeight: 7228, viewportHeight: 538,
            topInset: 116, bottomInset: 0
        )
        #expect(geometry.offset == 6690)
        #expect(geometry.bottomDistance == 0)
    }

    @Test func bottomBounceAndShortContentHaveZeroRemainingDistance() {
        for (offset, height): (CGFloat, CGFloat) in [(1124, 2000), (-116, 200)] {
            let geometry = TranscriptScrollGeometry(
                contentOffset: offset, contentHeight: height, viewportHeight: 800,
                topInset: 116, bottomInset: 160
            )
            #expect(geometry.bottomDistance == 0)
            var state = TranscriptFollowState()
            state.detach()
            state.positionChanged(bottomDistance: geometry.bottomDistance, isUserDriven: true)
            #expect(state.isFollowing)
        }
    }

    @Test func finalDecelerationPositionResumesButLayoutChangesDoNot() {
        var state = TranscriptFollowState()
        state.detach()
        state.recordNewEvents(3)
        state.positionChanged(bottomDistance: 49, isUserDriven: true)
        #expect(!state.isFollowing)
        state.positionChanged(bottomDistance: 0, isUserDriven: false)
        #expect(!state.isFollowing)
        // The final phase-change geometry may reach the threshold after the
        // last dragging/decelerating geometry callback.
        state.scrollingEnded(bottomDistance: 48)
        #expect(state.isFollowing)
        #expect(state.newEventCount == 0)
        #expect(!state.hasUnseenContent)
    }

    @Test func contentArrivingAtDecelerationEndCannotDetachFollowing() {
        var state = TranscriptFollowState()
        state.detach()
        state.positionChanged(bottomDistance: 0, isUserDriven: true)
        #expect(state.isFollowing)
        let followsBeforeEnd = state.contentChanged()
        #expect(followsBeforeEnd)
        // A new 50pt row arrives between reaching bottom and the idle callback.
        state.positionChanged(bottomDistance: 50, isUserDriven: false)
        state.scrollingEnded(bottomDistance: 50)
        #expect(state.isFollowing)
        let followsAfterEnd = state.contentChanged()
        #expect(followsAfterEnd)

        state.positionChanged(bottomDistance: 100, isUserDriven: true)
        state.scrollingEnded(bottomDistance: 100)
        #expect(!state.isFollowing)
    }

    @Test func appendThenFurtherMovementOrBounceCannotUndoManualReturn() {
        for remainingDistance: CGFloat in [95, 100] {
            var state = TranscriptFollowState()
            state.detach()
            state.positionChanged(bottomDistance: 0, isUserDriven: true)
            state.layoutChanged(bottomDistanceChange: 100)
            state.positionChanged(bottomDistance: 100, isUserDriven: false)
            // Continue toward the old end, or finish bouncing back to it.
            state.positionChanged(bottomDistance: remainingDistance, isUserDriven: true)
            #expect(state.isFollowing)
            // Genuine upward movement past the threshold still detaches.
            state.positionChanged(bottomDistance: 148, isUserDriven: true)
            #expect(state.isFollowing)
            state.positionChanged(bottomDistance: 149, isUserDriven: true)
            #expect(!state.isFollowing)
        }
    }

    @Test func reachingNewEndResetsThresholdBeforeReversingUpward() {
        var state = TranscriptFollowState()
        state.layoutChanged(bottomDistanceChange: 100)
        state.positionChanged(bottomDistance: 0, isUserDriven: true)
        state.positionChanged(bottomDistance: 49, isUserDriven: true)
        #expect(!state.isFollowing)
    }

    @Test func appendDuringBottomBounceKeepsFollowingUntilDecelerationEnds() {
        var state = TranscriptFollowState()
        state.layoutChanged(bottomDistanceChange: 50)
        // An append while overscrolled leaves 23pt to the new end. The bounce
        // then returns to the old end, 50pt above the new end, without a drag.
        state.positionChanged(bottomDistance: 23, isUserDriven: true, isDecelerating: true)
        state.positionChanged(bottomDistance: 50, isUserDriven: true, isDecelerating: true)
        #expect(state.isFollowing)
        state.scrollingEnded(bottomDistance: 50)
        #expect(state.isFollowing)
        state.scrollingBegan()
        state.positionChanged(bottomDistance: 49, isUserDriven: true)
        #expect(!state.isFollowing)
    }

    @Test func interruptingDecelerationStartsANewScrollThreshold() {
        var state = TranscriptFollowState()
        state.layoutChanged(bottomDistanceChange: 100)
        state.positionChanged(bottomDistance: 95, isUserDriven: true, isDecelerating: true)
        #expect(state.isFollowing)
        state.scrollingBegan()
        state.positionChanged(bottomDistance: 49, isUserDriven: true)
        #expect(!state.isFollowing)
    }

    @Test func removingJumpClearanceCannotCreateANegativeThreshold() {
        var state = TranscriptFollowState()
        state.layoutChanged(bottomDistanceChange: -61)
        state.positionChanged(bottomDistance: 0, isUserDriven: true)
        #expect(state.isFollowing)
    }

    @Test func finishingGestureClearsLayoutCompensation() {
        var state = TranscriptFollowState()
        state.layoutChanged(bottomDistanceChange: 100)
        state.scrollingEnded(bottomDistance: 100)
        state.positionChanged(bottomDistance: 49, isUserDriven: true)
        #expect(!state.isFollowing)
    }

    @Test func opensFollowingAndOnlyUserScrollingDetaches() {
        var state = TranscriptFollowState()
        #expect(state.isFollowing)
        state.positionChanged(bottomDistance: 500, isUserDriven: false)
        let shouldScroll = state.contentChanged()
        #expect(shouldScroll)
        state.positionChanged(bottomDistance: 49, isUserDriven: true)
        #expect(!state.isFollowing)
        let shouldScrollDetached = state.contentChanged()
        #expect(!shouldScrollDetached)
        state.recordNewEvents(3)
        state.recordNewEvents(2)
        #expect(state.newEventCount == 5)
        #expect(state.hasUnseenContent)
    }

    @Test func jumpAndManualReturnResumeFollowingAndClearUnread() {
        for jump in [true, false] {
            var state = TranscriptFollowState()
            state.detach()
            state.recordNewEvents(4)
            if jump {
                state.jumpToLatest()
            } else {
                state.positionChanged(bottomDistance: 48, isUserDriven: true)
            }
            #expect(state.isFollowing)
            #expect(state.newEventCount == 0)
            #expect(!state.hasUnseenContent)
            let shouldScroll = state.contentChanged()
            #expect(shouldScroll)
        }
    }

    @Test func layoutChangesCannotResumeDetachedReader() {
        var state = TranscriptFollowState()
        state.detach()
        state.positionChanged(bottomDistance: 0, isUserDriven: false)
        #expect(!state.isFollowing)
        let shouldScrollDetached = state.contentChanged()
        #expect(!shouldScrollDetached)
        #expect(state.hasUnseenContent)
        #expect(state.newEventCount == 0)
    }

    @Test func preservesPartialRowWhenEarlierContentOrCurrentRowGrows() {
        var state = TranscriptFollowState()
        state.detach()
        let frames = [
            "earlier": CGRect(x: 0, y: 0, width: 300, height: 100),
            "reading": CGRect(x: 0, y: 108, width: 300, height: 500),
        ]
        state.captureAnchor(in: frames, offset: 208)
        var grown = frames
        grown["earlier"]?.size.height += 150
        grown["reading"]?.origin.y += 150
        #expect(state.preservedOffset(in: grown) == 358)
        grown["reading"]?.size.height += 400
        #expect(state.preservedOffset(in: grown) == 358)
        state.jumpToLatest()
        #expect(state.preservedOffset(in: grown) == nil)
    }

    @Test func nextUserScrollReplacesTheReadingAnchor() {
        var state = TranscriptFollowState()
        state.detach()
        let frames = [
            "first": CGRect(x: 0, y: 0, width: 300, height: 200),
            "second": CGRect(x: 0, y: 208, width: 300, height: 200),
        ]
        state.captureAnchor(in: frames, offset: 50)
        state.captureAnchor(in: frames, offset: 250)
        var grown = frames
        grown["second"]?.origin.y += 80
        #expect(state.preservedOffset(in: grown) == 330)
    }
}
