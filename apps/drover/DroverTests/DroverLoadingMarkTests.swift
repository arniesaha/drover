import SwiftUI
import Testing
@testable import Drover

/// The cold-open indicator's contract: when it shows, when it stops, and which
/// ramp it draws in.
///
/// The behaviour is the whole component — the spinner itself is the platform's.
/// What is worth locking is the delay that keeps a fast open silent, and the
/// fact that this state ends for good once a session has attached.
struct DroverLoadingMarkTests {
    // MARK: - When it shows

    /// A local open lands in tens of milliseconds. A spinner that flashed on
    /// every one of those would read as jank rather than as reassurance.
    @Test func aFastOpenNeverShowsIt() {
        #expect(DroverLoadingMark.shouldShow(hasConnectedOnce: false, elapsed: 0.0) == false)
        #expect(DroverLoadingMark.shouldShow(hasConnectedOnce: false, elapsed: 0.2) == false)
    }

    @Test func aSlowOpenShowsItOnceTheDelayPasses() {
        #expect(DroverLoadingMark.shouldShow(hasConnectedOnce: false, elapsed: 0.3) == true)
        #expect(DroverLoadingMark.shouldShow(hasConnectedOnce: false, elapsed: 2.0) == true)
    }

    /// Reconnects are the pill's job, not this one's. Once a session has
    /// attached even once, this indicator is done for good — otherwise every
    /// dropped socket would put a spinner over a transcript you can already
    /// read.
    @Test func itIsDoneForeverOnceTheSessionHasAttached() {
        for elapsed in [0.0, 0.3, 30.0] {
            #expect(DroverLoadingMark.shouldShow(hasConnectedOnce: true, elapsed: elapsed) == false,
                    "still showing at \(elapsed)s after the session had attached")
        }
    }

    /// The threshold is a judgement call, but a wildly wrong one is a bug: too
    /// short and it flashes, too long and the screen sits empty through the
    /// wait it exists to cover.
    @Test func theDelayIsShortEnoughToStillCoverTheWait() {
        #expect(DroverLoadingMark.appearAfter >= 0.15)
        #expect(DroverLoadingMark.appearAfter <= 0.5)
    }

    // MARK: - Both ramps

    /// The design system's rule is that no component branches on the theme —
    /// a token resolves its own ramp.
    @Test func theSpinnerTakesTheAccentInBothRamps() {
        for scheme in [ColorScheme.dark, .light] {
            #expect(DroverLoadingMark.tint.rgb(for: scheme)
                    == DroverColor.accent.rgb(for: scheme),
                    "the indicator drifted off the accent token in \(scheme)")
        }
    }

    @Test func theTintIsNotTheSameColourInBothRamps() {
        #expect(DroverLoadingMark.tint.dark != DroverLoadingMark.tint.light)
    }
}
