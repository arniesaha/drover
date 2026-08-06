import Foundation

/// Compact token formatting shared by the usage footer, the thinking row,
/// and the context gauge: 158148 -> "158.1K", 1000000 -> "1M".
public enum TokenCount {
    public static func format(_ value: Int) -> String {
        let absolute = abs(value)
        if absolute >= 1_000_000 { return compact(Double(value) / 1_000_000, suffix: "M") }
        if absolute >= 1_000 { return compact(Double(value) / 1_000, suffix: "K") }
        return "\(value)"
    }

    private static func compact(_ value: Double, suffix: String) -> String {
        let rounded = (value * 10).rounded() / 10
        if rounded.truncatingRemainder(dividingBy: 1) == 0 {
            return "\(Int(rounded))\(suffix)"
        }
        return String(format: "%.1f%@", rounded, suffix)
    }
}
