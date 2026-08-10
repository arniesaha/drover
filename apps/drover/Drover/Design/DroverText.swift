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
    ///
    /// `accented` raises the role to `accentHi`, for the handful of places
    /// that carry a signal rather than prose — a quota nearly spent, a state
    /// worth looking at. It is a parameter rather than a `.foregroundStyle`
    /// layered on afterwards because the style nearest the `Text` is the one
    /// SwiftUI draws, so layering silently does nothing.
    func droverText(_ style: DroverTextStyle, accented: Bool = false) -> some View {
        modifier(DroverTextModifier(style: style, accented: accented))
    }
}

private struct DroverTextModifier: ViewModifier {
    let style: DroverTextStyle
    var accented = false

    func body(content: Content) -> some View {
        content
            .font(font)
            .tracking(tracking)
            .textCase(style == .h3 ? .uppercase : nil)
            // One `foregroundStyle`, chosen — not a default with an override
            // layered on top, which SwiftUI resolves the other way round.
            .foregroundStyle(accented ? DroverColor.accentHi : foreground)
    }

    private var font: Font {
        switch style {
        case .h1: .system(.title3, design: .default, weight: .medium)
        case .h2: .system(.callout, design: .default, weight: .medium)
        case .h3: .system(.caption2, design: .default, weight: .medium)
        case .body: .system(.subheadline, design: .default)
        case .nested: .system(.footnote, design: .default)
        case .subtitle: .system(.caption, design: .default)
        case .mono: .system(.caption, design: .monospaced)
        case .marker: .system(.caption, design: .monospaced).monospacedDigit()
        }
    }

    private var tracking: CGFloat {
        switch style {
        case .h1: -0.38
        case .h2: -0.19
        case .h3: 1.4
        default: 0
        }
    }

    private var foreground: PaletteToken {
        switch style {
        case .h1, .h2, .body: DroverColor.text
        case .h3, .nested, .subtitle: DroverColor.muted
        case .mono: DroverColor.faint
        case .marker: DroverColor.accentHi
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
