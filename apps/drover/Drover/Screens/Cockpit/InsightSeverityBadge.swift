import DroverKit
import SwiftUI

/// Severity remains distinguishable without color, through symbol and text.
struct InsightSeverityBadge: View {
    let severity: InsightSeverity

    var body: some View {
        Label(severity.rawValue.capitalized, systemImage: symbol)
            .droverText(.subtitle, accented: severity == .critical || severity == .high)
            .padding(.horizontal, 8)
            .padding(.vertical, 5)
            .background(DroverColor.bg, in: Capsule())
            .overlay { Capsule().strokeBorder(DroverColor.line, lineWidth: 1) }
    }

    private var symbol: String {
        switch severity {
        case .critical: "exclamationmark.octagon.fill"
        case .high: "exclamationmark.triangle"
        case .medium: "diamond"
        case .low: "info.circle"
        }
    }
}
