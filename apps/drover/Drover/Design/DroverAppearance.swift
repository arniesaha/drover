import SwiftUI

/// Which ramp the app draws in, as an explicit preference rather than whatever
/// the phone happens to be set to.
///
/// The palette was already theme-blind — every token resolves its own ramp
/// from `colorScheme` (see `DroverColor`) — so a preference here is the only
/// piece that was missing: it feeds `.preferredColorScheme` at the composition
/// root, and the environment carries it to every token unchanged.
enum DroverAppearance: String, CaseIterable, Sendable {
    /// Follow iOS. The default, and what a fresh install starts on.
    case system
    case light
    case dark

    /// `nil` hands the decision back to iOS.
    var colorScheme: ColorScheme? {
        switch self {
        case .system: nil
        case .light: .light
        case .dark: .dark
        }
    }

    /// What the control does next, given the ramp actually on screen.
    ///
    /// Deliberately keyed on what is *displayed* rather than on `self`: from
    /// `.system` there is no stored side to flip, and flipping to the mode the
    /// user is already looking at would make the first tap do nothing visible.
    func toggled(displaying scheme: ColorScheme) -> DroverAppearance {
        scheme == .dark ? .light : .dark
    }

    /// The design shows the mode you are about to get, not the one you are in:
    /// a sun while dark, a moon while light.
    static func symbolName(displaying scheme: ColorScheme) -> String {
        scheme == .dark ? "sun.max" : "moon"
    }

    static func accessibilityLabel(displaying scheme: ColorScheme) -> String {
        scheme == .dark ? "Switch to light mode" : "Switch to dark mode"
    }
}

/// The preference, persisted. Small enough to be a store rather than an
/// `@AppStorage` at the call site because two views need the same value — the
/// root applies it, the inbox header toggles it — and because a seam for
/// `UserDefaults` is what lets the cycle be tested without a running app.
@Observable
final class AppearanceStore {
    private static let defaultsKey = "drover.appearance"

    var appearance: DroverAppearance {
        didSet {
            guard appearance != oldValue else { return }
            defaults.set(appearance.rawValue, forKey: Self.defaultsKey)
        }
    }

    private let defaults: UserDefaults

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        // An unreadable or unknown stored value falls back to `.system`
        // rather than to a hardcoded ramp — the phone's own setting is a
        // better guess than either of ours.
        appearance = defaults.string(forKey: Self.defaultsKey)
            .flatMap(DroverAppearance.init(rawValue:)) ?? .system
    }

    func toggle(displaying scheme: ColorScheme) {
        appearance = appearance.toggled(displaying: scheme)
    }
}
