import Foundation

public enum AccountQuotaPresentation {
    public static func remainingText(usedPercent: Double?) -> String {
        guard let usedPercent else { return "quota unknown" }
        return "\(Int(max(0, min(100, (100 - usedPercent)).rounded())))% left"
    }
}
