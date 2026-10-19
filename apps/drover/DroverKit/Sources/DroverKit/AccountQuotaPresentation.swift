import Foundation

public enum AccountQuotaPresentation {
    public static func remainingText(usedPercent: Double?) -> String {
        guard let usedPercent else { return "quota unknown" }
        return "\(Int(max(0, min(100, (100 - usedPercent)).rounded())))% left"
    }

    /// The quota most relevant to a harness running on a particular host.
    /// A host can report more than one window (or account) for its provider;
    /// show the tightest percentage so the picker never overstates capacity.
    public static func remainingText(
        harness: String,
        hostID: String,
        accounts: [ProviderAccount]
    ) -> String {
        let normalizedHarness = harness.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        let provider: String
        switch normalizedHarness {
        case "claude-code", "claude": provider = "anthropic"
        case "codex": provider = "openai"
        case "agy", "antigravity", "gemini": provider = "google"
        default: provider = normalizedHarness
        }

        let usedPercent = accounts
            .filter { $0.hostID == hostID && $0.provider.lowercased() == provider }
            .flatMap(\.windows)
            .compactMap(\.usedPercent)
            .max()
        return remainingText(usedPercent: usedPercent)
    }
}
