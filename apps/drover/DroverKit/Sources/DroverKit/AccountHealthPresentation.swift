import Foundation

/// The one actionable account condition shown in Accounts and represented by
/// the Accounts dock badge. Keeping this policy in DroverKit means Home cannot
/// grow a second, divergent account-warning surface.
public enum AccountHealthPresentation {
    public struct Attention: Sendable, Equatable {
        public enum Kind: Int, Sendable, Equatable {
            case authentication = 0
            case error = 1
            case stale = 2
            case lowQuota = 3
        }

        public let kind: Kind
        /// Short, provider-independent text for the account card.
        public let reason: String

        public init(kind: Kind, reason: String) {
            self.kind = kind
            self.reason = reason
        }
    }

    /// A subscription can be reported by several hosts. It is counted once in
    /// the dock, using its most actionable condition, while Accounts keeps all
    /// host detail available on the card.
    public static func attention(for accounts: [ProviderAccount]) -> Attention? {
        accounts.compactMap(attention(for:)).min { lhs, rhs in
            lhs.kind.rawValue < rhs.kind.rawValue
        }
    }

    public static func attention(for account: ProviderAccount) -> Attention? {
        let category = account.errorCategory?.lowercased()
        if category == "login_expired" || category == "login_refresh_needed" {
            return Attention(kind: .authentication, reason: "login expired")
        }
        if account.status == .error || account.status == .unknown {
            return Attention(kind: .error, reason: "account error")
        }
        if account.status == .stale {
            return Attention(kind: .stale, reason: "stale")
        }
        if let lowestRemaining = account.windows.compactMap({ window -> Double? in
            guard let used = window.usedPercent else { return nil }
            return used > 80 ? 100 - used : nil
        }).min() {
            return Attention(
                kind: .lowQuota,
                reason: "\(Int(lowestRemaining.rounded()))% left"
            )
        }
        return nil
    }

    /// Badge counts are account counts, not host-probe counts. Grouping here
    /// also makes the count agree exactly with the cards shown in Accounts.
    public static func count(accounts: [ProviderAccount]) -> Int {
        ProviderSubscriptionGrouping.group(accounts).filter { $0.attention != nil }.count
    }

    public static func badgeValue(for count: Int) -> String? {
        count > 0 ? String(count) : nil
    }
}
