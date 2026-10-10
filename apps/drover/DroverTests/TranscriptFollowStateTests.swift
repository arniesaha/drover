import Foundation
import Testing
@testable import Drover

struct TranscriptFollowStateTests {
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
