import DroverKit
import SwiftUI

/// A distribution list: the facts common to every row said once in the
/// heading, and rows that carry only what varies.
///
/// The shipped version printed source, freshness and coverage on every row, so
/// five rows filled the screen and the shape of the data never appeared. Here
/// the heading holds those, and each row is one line plus a share bar.
struct DistributionSectionView: View {
    let section: DistributionSectionPresentation
    let glyph: String
    let onToggleRank: () -> Void
    var trailing: AnyView?

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            // Wraps rather than sharing one line. At accessibility sizes the
            // title and the toggle together exceed the width, and squeezing
            // them side by side hyphenated the heading into "HARNESS-ES".
            FlowLayout(spacing: 8, lineSpacing: 4) {
                Text(section.title)
                    .droverText(.h3)
                // Naming the ranking and offering the other in one control:
                // sessions and tokens disagree constantly, and the screen
                // should let you see that rather than pick one silently.
                Button(action: onToggleRank) {
                    Label(section.rank.toggleTitle, systemImage: "arrow.up.arrow.down")
                        .font(.system(.caption, design: .default, weight: .medium))
                        .foregroundStyle(DroverColor.accentHi)
                        .padding(.horizontal, 10)
                        .frame(minHeight: 32)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Rank by \(section.rank.other.noun)")
                .accessibilityIdentifier("analytics-rank-toggle-\(section.title.lowercased())")
            }

            Text(section.subtitle)
                .droverText(.subtitle)
                .fixedSize(horizontal: false, vertical: true)

            ForEach(section.rows) { row in
                DistributionRow(row: row, glyph: glyph)
            }

            if let trailing {
                trailing
            }
        }
    }
}

/// One row: glyph, name, the ranked number, a share bar, one detail line.
struct DistributionRow: View {
    let row: DistributionRowPresentation
    let glyph: String
    /// The glyph column has to grow with the text, or at accessibility sizes
    /// the icon overflows a fixed 18pt slot and collides with the name.
    @ScaledMetric(relativeTo: .footnote) private var glyphWidth: CGFloat = 18

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            // The glyph replaces the repeated noun ("harness", "host"), and
            // never travels alone — the name sits beside it and the full
            // wording is on the row's accessibility label.
            Image(systemName: glyph)
                .font(.system(.footnote, design: .default))
                .foregroundStyle(DroverColor.faint)
                .frame(width: glyphWidth)
                .padding(.top, 2)

            VStack(alignment: .leading, spacing: 5) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(row.title)
                        .droverText(.body)
                        .lineLimit(2)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 8)
                    // The number is always beside the bar, so the bar is
                    // never the only way to read the quantity.
                    Text(row.valueText)
                        .droverText(.body)
                        .monospacedDigit()
                }

                // `nil` draws CapacityBar's dashed track: "unreported" and
                // "nothing" are opposite statements and must not look alike.
                CapacityBar(
                    fraction: row.tokensUnreported && row.shareFraction == 0
                        ? nil : row.shareFraction,
                    height: 4
                )

                HStack(spacing: 6) {
                    Text(row.tokensUnreported ? "— \(row.detailText)" : row.detailText)
                        .droverText(.subtitle)
                        .fixedSize(horizontal: false, vertical: true)
                    // A row only carries its own age when it disagrees with
                    // the heading; otherwise the heading already said it.
                    if let age = row.ageText {
                        Image(systemName: row.isStale ? "circle.dotted" : "clock")
                            .font(.system(.caption2, design: .default))
                            .foregroundStyle(DroverColor.faint)
                        Text(age)
                            .droverText(.subtitle)
                            .monospacedDigit()
                    }
                }

                // Kept because it differs row to row; the collapse targets
                // text that repeated identically down the whole list.
                if let secondary = row.secondaryText {
                    Text(secondary)
                        .droverText(.nested)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        // Every row clears the 44pt minimum target even at the smallest type.
        .frame(minHeight: 44)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(DroverColor.surface, in: RoundedRectangle(cornerRadius: 10))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(row.accessibilityLabel)
    }
}
