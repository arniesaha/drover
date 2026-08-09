import SwiftUI
import Testing
@testable import Drover

/// The theme control's contract: one tap always changes what you are looking
/// at, the choice survives a relaunch, and the glyph promises the mode you are
/// about to get.
struct DroverAppearanceTests {
    private func store() -> AppearanceStore {
        // A throwaway suite per test — `.standard` would leak a real user's
        // preference into the run (and the run's into theirs).
        let defaults = UserDefaults(suiteName: "drover.appearance.tests.\(UUID().uuidString)")!
        return AppearanceStore(defaults: defaults)
    }

    // MARK: - Defaults

    /// A fresh install has no opinion — iOS does.
    @Test func aFreshInstallFollowsTheSystem() {
        #expect(store().appearance == .system)
        #expect(DroverAppearance.system.colorScheme == nil)
    }

    @Test func anExplicitChoiceResolvesToThatRamp() {
        #expect(DroverAppearance.light.colorScheme == .light)
        #expect(DroverAppearance.dark.colorScheme == .dark)
    }

    // MARK: - The toggle

    /// The bug this exists to prevent: from `.system` there is no stored side
    /// to flip, so a toggle written as "not self" lands on whichever mode the
    /// user is *already* in and the first tap does nothing visible.
    @Test func theFirstTapAlwaysLeavesTheModeOnScreen() {
        for scheme in [ColorScheme.dark, .light] {
            let preference = DroverAppearance.system.toggled(displaying: scheme)
            #expect(preference.colorScheme != scheme,
                    "tapping from system while \(scheme) stayed on \(scheme)")
        }
    }

    @Test func togglingTwiceReturnsToTheModeYouStartedIn() {
        let appearance = store()
        appearance.toggle(displaying: .dark)
        #expect(appearance.appearance == .light)
        appearance.toggle(displaying: .light)
        #expect(appearance.appearance == .dark)
    }

    // MARK: - Persistence

    @Test func theChoiceSurvivesARelaunch() {
        let defaults = UserDefaults(suiteName: "drover.appearance.tests.\(UUID().uuidString)")!
        AppearanceStore(defaults: defaults).toggle(displaying: .dark)

        #expect(AppearanceStore(defaults: defaults).appearance == .light)
    }

    /// A value written by a future build (or corrupted on disk) must not
    /// strand the app on a ramp it cannot name.
    @Test func anUnknownStoredValueFallsBackToTheSystem() {
        let defaults = UserDefaults(suiteName: "drover.appearance.tests.\(UUID().uuidString)")!
        defaults.set("sepia", forKey: "drover.appearance")

        #expect(AppearanceStore(defaults: defaults).appearance == .system)
    }

    // MARK: - The glyph

    /// The design shows the destination, not the current state: a sun offers
    /// you light while you are in the dark.
    @Test func theGlyphPromisesWhereTheTapTakesYou() {
        #expect(DroverAppearance.symbolName(displaying: .dark) == "sun.max")
        #expect(DroverAppearance.symbolName(displaying: .light) == "moon")
        #expect(DroverAppearance.accessibilityLabel(displaying: .dark) == "Switch to light mode")
        #expect(DroverAppearance.accessibilityLabel(displaying: .light) == "Switch to dark mode")
    }
}
