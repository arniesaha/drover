import Foundation

/// Navigation destinations available in the primary bottom dock.
public enum AppTab: String, CaseIterable, Identifiable, Sendable {
    case analytics = "analytics"
    case insights = "insights"
    case accounts = "accounts"
    case home = "home"

    public var id: String { rawValue }

    public var title: String {
        switch self {
        case .analytics: "Analytics"
        case .insights: "Insights"
        case .accounts: "Accounts"
        case .home: "Home"
        }
    }

    public var systemImage: String {
        switch self {
        case .analytics: "chart.bar"
        case .insights: "lightbulb"
        case .accounts: "person.crop.circle"
        case .home: "rectangle.stack"
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
