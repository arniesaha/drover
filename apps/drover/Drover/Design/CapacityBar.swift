import SwiftUI

/// A quota window's utilisation, as a bar.
///
/// Severity is carried by *weight* rather than by hue, because Tide has exactly
/// one accent and no per-state palette — see the note in `DroverColor` on why a
/// pale warning fill disappears on the light ground. A nearly-exhausted window
/// gets an accent outline around the track; it does not turn amber.
struct CapacityBar: View {
    /// Used, 0...1. Nil draws an empty dashed track: "we have no reading" and
    /// "nothing used" are opposite statements and must not look alike.
    let fraction: Double?
    var isCritical: Bool = false
    var height: CGFloat = 6

    var body: some View {
        Capsule()
            .fill(DroverColor.line)
            .frame(height: height)
            .overlay(alignment: .leading) {
                if let fraction {
                    GeometryReader { geometry in
                        Capsule()
                            .fill(DroverColor.accent)
                            .frame(width: fillWidth(in: geometry.size.width, fraction: fraction))
                    }
                }
            }
            .overlay {
                if fraction == nil {
                    Capsule().strokeBorder(
                        DroverColor.faint,
                        style: StrokeStyle(lineWidth: 1, dash: [3, 3])
                    )
                } else if isCritical {
                    Capsule().strokeBorder(DroverColor.accent, lineWidth: 1)
                }
            }
            // The figure beside the bar already says this, and the card reads
            // as one combined element.
            .accessibilityHidden(true)
    }

    private func fillWidth(in width: CGFloat, fraction: Double) -> CGFloat {
        guard fraction > 0 else { return 0 }
        // A capsule narrower than it is tall collapses to nothing, so a window
        // at 1% has to clamp up to a visible dot rather than read as unused.
        return max(height, width * fraction)
    }
}
