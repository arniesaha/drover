import SwiftUI

/// The Nocturne type ramp, expressed as *relative* text styles.
///
/// The design specifies the ramp in fixed pixels (21/16/15/14, mono 11.5).
/// Fixed sizes opt out of Dynamic Type, so each role is mapped to the system
/// text style nearest its specified size and scales from there; the
/// proportions, weights and tracking are what carry the design, not the
/// absolute numbers. SF Pro replaces Inter — metrically different, same ramp.
///
/// The system guide is emphatic that headings never go past weight 500
/// ("hierarchy here is size and space"), which is why nothing below is
/// semibold or bold — including the card title, which used to be.
enum DroverTextStyle {
    /// Answer headings. 21/1.25, −.018em.
    case h1
    /// Sub-headings. 16/1.3, −.012em.
    case h2
    /// Small caps label, muted. 10/1, .14em tracking.
    case h3
    /// Answer body and the loud line on a card. 15/1.55.
    case body
    /// Nested/secondary prose, muted. 14/1.5.
    case nested
    /// The quiet line under a card title.
    case subtitle
    /// Machine strings — paths, branches, commands, table cells.
    case mono
    /// List markers and step counts: tabular mono in the accent.
    case marker
}

extension View {
    /// Applies one ramp role — font, tracking, case and colour together, so a
    /// call site never picks a colour and a size separately and drifts.
    func droverText(_ style: DroverTextStyle) -> some View {
        modifier(DroverTextModifier(style: style))
    }
}

private struct DroverTextModifier: ViewModifier {
    let style: DroverTextStyle

    func body(content: Content) -> some View {
        switch style {
        case .h1:
            content
                .font(.system(.title3, design: .default, weight: .medium))
                .tracking(-0.38)
                .foregroundStyle(DroverColor.text)
        case .h2:
            content
                .font(.system(.callout, design: .default, weight: .medium))
                .tracking(-0.19)
                .foregroundStyle(DroverColor.text)
        case .h3:
            content
                .font(.system(.caption2, design: .default, weight: .medium))
                .tracking(1.4)
                .textCase(.uppercase)
                .foregroundStyle(DroverColor.muted)
        case .body:
            content
                .font(.system(.subheadline, design: .default))
                .foregroundStyle(DroverColor.text)
        case .nested:
            content
                .font(.system(.footnote, design: .default))
                .foregroundStyle(DroverColor.muted)
        case .subtitle:
            content
                .font(.system(.caption, design: .default))
                .foregroundStyle(DroverColor.muted)
        case .mono:
            content
                .font(.system(.caption, design: .monospaced))
                .foregroundStyle(DroverColor.faint)
        case .marker:
            content
                .font(.system(.caption, design: .monospaced).monospacedDigit())
                .foregroundStyle(DroverColor.accentHi)
        }
    }
}

/// A freestanding rule that fades to transparent at both ends — a Nocturne
/// signature ("rules fade over 48px a side rather than stopping cleanly").
/// Box outlines and in-control separators stay solid and use `DroverColor.line`
/// directly; this is only for rules standing on their own.
struct FadingRule: View {
    var body: some View {
        GeometryReader { geometry in
            // The 48px ramp is a fixed distance from each end, so it has to be
            // expressed as a fraction of the actual width rather than a
            // gradient stop — at phone widths a hardcoded stop would leave no
            // solid middle at all.
            let fade = min(48 / max(geometry.size.width, 1), 0.45)
            LinearGradient(
                stops: [
                    .init(color: .clear, location: 0),
                    .init(color: DroverColor.line.color(for: colorScheme), location: fade),
                    .init(color: DroverColor.line.color(for: colorScheme), location: 1 - fade),
                    .init(color: .clear, location: 1),
                ],
                startPoint: .leading,
                endPoint: .trailing
            )
        }
        .frame(height: 1)
    }

    @Environment(\.colorScheme) private var colorScheme
}
