import Foundation

/// Pure Home-warning policy.  Views supply only the already-decoded account facts.
public enum AccountHealthPresentation {
    public static func needsAttention(
        usedPercents: [Double?],
        hasAuthError: Bool,
        hasProviderError: Bool,
        isStale: Bool
    ) -> Bool {
        hasAuthError || hasProviderError || isStale || usedPercents.contains { ($0 ?? 0) > 80 }
    }
}
