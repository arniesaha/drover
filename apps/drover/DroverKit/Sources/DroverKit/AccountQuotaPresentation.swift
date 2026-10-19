import Foundation

public enum AccountQuotaPresentation {
    public static func remainingText(usedPercent: Double?) -> String {
        guard let usedPercent else { return "quota unknown" }
        return "\(Int(max(0, min(100, (100 - usedPercent)).rounded())))% left"
    }

    /// The quota associated with a launcher running on a particular host.
    /// The hub supplies that association on each provider account. A host can
    /// report more than one window (or account) for a launcher;
    /// show the tightest percentage so the picker never overstates capacity.
    public static func remainingText(
        harness: String,
        hostID: String,
        accounts: [ProviderAccount]
    ) -> String {
        let usedPercent = accounts
            .filter { $0.hostID == hostID && $0.harnesses.contains(harness) }
            .flatMap(\.windows)
            .compactMap(\.usedPercent)
            .max()
        return remainingText(usedPercent: usedPercent)
    }
}
