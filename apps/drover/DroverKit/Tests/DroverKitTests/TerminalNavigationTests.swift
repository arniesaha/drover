import Testing
@testable import DroverKit

struct TerminalNavigationTests {
    @Test func movementInsideDeadZoneDoesNothing() {
        var repeater = TerminalNavigationRepeater()

        #expect(repeater.update(horizontal: 11, vertical: -7) == nil)
        #expect(repeater.repeatedDirection() == nil)
    }

    @Test func dominantAxisChoosesTheArrowDirection() {
        var repeater = TerminalNavigationRepeater()

        #expect(repeater.update(horizontal: 34, vertical: -20) == .right)
        #expect(repeater.update(horizontal: 18, vertical: -45) == .up)
        #expect(repeater.update(horizontal: -51, vertical: 12) == .left)
        #expect(repeater.update(horizontal: 4, vertical: 39) == .down)
    }

    @Test func distanceSelectsThreeIncreasingRepeatSpeeds() throws {
        var repeater = TerminalNavigationRepeater()

        _ = repeater.update(horizontal: 28, vertical: 0)
        let slow = try #require(repeater.motion)
        _ = repeater.update(horizontal: 72, vertical: 0)
        let medium = try #require(repeater.motion)
        _ = repeater.update(horizontal: 132, vertical: 0)
        let fast = try #require(repeater.motion)

        #expect(slow.gear == .slow)
        #expect(medium.gear == .medium)
        #expect(fast.gear == .fast)
        #expect(slow.repeatInterval > medium.repeatInterval)
        #expect(medium.repeatInterval > fast.repeatInterval)
    }

    @Test func heldDirectionRepeatsUntilTheGestureStops() {
        var repeater = TerminalNavigationRepeater()

        #expect(repeater.update(horizontal: 40, vertical: 3) == .right)
        #expect(repeater.repeatedDirection() == .right)
        #expect(repeater.update(horizontal: 42, vertical: 4) == nil)
        #expect(repeater.repeatedDirection() == .right)

        repeater.stop()

        #expect(repeater.motion == nil)
        #expect(repeater.repeatedDirection() == nil)
    }

    @Test func changingDirectionOrGearSendsImmediately() {
        var repeater = TerminalNavigationRepeater()

        #expect(repeater.update(horizontal: 30, vertical: 0) == .right)
        #expect(repeater.update(horizontal: 80, vertical: 0) == .right)
        #expect(repeater.update(horizontal: 4, vertical: 80) == .down)
    }
}
