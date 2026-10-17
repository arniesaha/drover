import Foundation

/// Headline figures, short enough to read at a glance.
///
/// `63,132,964` is nine digits and a shape you have to count rather than see;
/// beside two other metrics it also forces the row to wrap or shrink. A
/// headline answers "how much, roughly", so it abbreviates.
///
/// Detail lines keep their exact figure — a distribution row still prints
/// `23,832,216 tokens`, because there the number *is* the content and rounding
/// it would lose the comparison the row exists to support.
public enum CompactNumber {
    private struct Unit {
        let cutoff: Int
        let divisor: Double
        let suffix: String
    }

    private static let units: [Unit] = [
        Unit(cutoff: 999_500_000_000, divisor: 1_000_000_000_000, suffix: "T"),
        Unit(cutoff: 999_500_000, divisor: 1_000_000_000, suffix: "B"),
        Unit(cutoff: 999_500, divisor: 1_000_000, suffix: "M"),
        Unit(cutoff: 1_000, divisor: 1_000, suffix: "K"),
    ]

    /// `63_132_964` → `"63M"`, `999` → `"999"`, `1_500` → `"1.5K"`, `1_250_000_000` → `"1.2B"`.
    ///
    /// One decimal place when the scaled figure is under 10; zero decimals at 10 and above.
    /// Trailing `.0` is stripped (`1K`, not `1.0K`).
    public static func abbreviated(_ value: Int, locale: Locale = .autoupdatingCurrent) -> String {
        let sign = value < 0 ? "-" : ""
        let magnitude = abs(value)

        let formatter = NumberFormatter()
        formatter.locale = locale
        formatter.numberStyle = .decimal

        for unit in units {
            if magnitude >= unit.cutoff {
                let scaled = Double(magnitude) / unit.divisor
                if scaled < 9.95 {
                    formatter.maximumFractionDigits = 1
                    formatter.minimumFractionDigits = 0
                } else {
                    formatter.maximumFractionDigits = 0
                }
                let str = formatter.string(from: scaled as NSNumber) ?? ""
                return "\(sign)\(str)\(unit.suffix)"
            }
        }

        formatter.maximumFractionDigits = 0
        let str = formatter.string(from: magnitude as NSNumber) ?? "\(magnitude)"
        return "\(sign)\(str)"
    }

    /// Full formatted integer with locale grouping separator for tooltips and accessibility labels.
    /// e.g. `1,250,000,000` in en_US, `1.250.000.000` in de_DE.
    public static func exact(_ value: Int, locale: Locale = .autoupdatingCurrent) -> String {
        let formatter = NumberFormatter()
        formatter.locale = locale
        formatter.numberStyle = .decimal
        formatter.maximumFractionDigits = 0
        return formatter.string(from: value as NSNumber) ?? "\(value)"
    }
}

