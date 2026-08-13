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
    /// `63_132_964` → `"63.1M"`, `999` → `"999"`, `1_500` → `"1.5K"`.
    ///
    /// One decimal only while the mantissa has a single digit, so the result
    /// is never longer than four characters plus its suffix: `9.9K`, `63.1M`,
    /// `631M`, `1.2B`.
    public static func abbreviated(_ value: Int) -> String {
        let sign = value < 0 ? "-" : ""
        let magnitude = abs(value)

        for (threshold, suffix) in [
            (1_000_000_000_000, "T"),
            (1_000_000_000, "B"),
            (1_000_000, "M"),
            (1_000, "K"),
        ] where magnitude >= threshold {
            let scaled = Double(magnitude) / Double(threshold)
            // 9.9M keeps its decimal; 63.1M keeps one; 631M drops it, because
            // a tenth of a hundred-million is noise at a glance.
            let rendered = scaled < 100
                ? String(format: "%.1f", scaled)
                : String(Int(scaled.rounded()))
            // "1.0K" reads worse than "1K".
            let trimmed = rendered.hasSuffix(".0")
                ? String(rendered.dropLast(2))
                : rendered
            return "\(sign)\(trimmed)\(suffix)"
        }

        return "\(sign)\(magnitude)"
    }
}
