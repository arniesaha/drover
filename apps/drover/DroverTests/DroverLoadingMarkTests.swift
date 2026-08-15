import SwiftUI
import Testing
@testable import Drover

/// The cold-open indicator's contract: which ramp it draws in.
///
/// When it shows used to live here too, against `DroverLoadingMark.shouldShow`.
/// That decision moved to `ColdOpenTracker.state` in DroverKit when the
/// unreachable state was added (#170) — chat and terminal both need the same
/// answer, and a rule about when a screen gives up should not need a simulator
/// to test. Those cases moved with it, to `ColdOpenTests`.
struct DroverLoadingMarkTests {
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
