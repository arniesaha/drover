import Foundation

/// Navigation destinations available in the primary bottom dock.
public enum AppTab: String, CaseIterable, Identifiable, Sendable {
    case home = "home"
    case inbox = "inbox"
    case analytics = "analytics"
    case accounts = "accounts"

    public var id: String { rawValue }

    public var title: String {
        switch self {
        case .home: "Home"
        case .inbox: "Inbox"
        case .analytics: "Analytics"
        case .accounts: "Accounts"
        }
    }

    public var systemImage: String {
        switch self {
        case .home: "rectangle.stack"
        case .inbox: "tray.full"
        case .analytics: "chart.bar"
        case .accounts: "person.crop.circle"
        }
    }

    public var accessibilityIdentifier: String {
        "tab-\(rawValue)"
    }

    public static let defaultsKey = "drover.selected-tab"

    /// Loads the persisted selected tab, falling back to `fallback` (default `.home`).
    public static func load(from defaults: UserDefaults, default fallback: AppTab = .home) -> AppTab {
        guard let raw = defaults.string(forKey: defaultsKey),
              let tab = AppTab(rawValue: raw) else {
            return fallback
        }
        return tab
    }

    /// Persists the selected tab to user defaults.
    public func save(to defaults: UserDefaults) {
        defaults.set(rawValue, forKey: Self.defaultsKey)
    }
}
