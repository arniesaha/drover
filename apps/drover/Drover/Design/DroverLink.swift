import SwiftUI

/// The ground a link is being drawn on, which is what decides its colour.
///
/// SwiftUI draws link runs in the environment's tint and nothing else — not
/// the surrounding `foregroundStyle`, not the bubble's own palette. The app
/// tints everything with `DroverColor.accent`, and the user's chat bubble is
/// *filled* with that same accent, so a URL you sent was drawn in the exact
/// colour of the bubble behind it: a 1:1 contrast ratio, a link that renders
/// as a bubble-coloured gap in your own message.
///
/// So links pick their colour from what they sit on, and carry an underline
/// besides — colour alone is not an accessible way to mark a link, and the
/// underline is what survives both ramps and both grounds.
enum DroverLinkGround {
    /// The neutral ramp: assistant bubbles, quotes, list items, headings.
    case surface
    /// The user's own bubble, whose ground *is* the tint.
    case accent

    /// Deliberately not a `PaletteToken`: on the accent ground the only
    /// correct answer is the bubble's own foreground, which is white in both
    /// ramps because the ground is the same accent in both.
    func linkRGB(for scheme: ColorScheme) -> UInt32 {
        switch self {
        case .surface: DroverColor.accentHi.rgb(for: scheme)
        case .accent: 0xFF_FF_FF
        }
    }

    func linkColor(for scheme: ColorScheme) -> Color {
        Color(rgb: linkRGB(for: scheme))
    }
}

extension AttributedString {
    /// Recolours and underlines every link run for the ground it lands on.
    ///
    /// Applied at the leaf, not at decode: `DroverKit` parses the markdown and
    /// has no palette, and the same parsed string renders on both grounds
    /// depending on who sent it.
    func droverLinks(on ground: DroverLinkGround, in scheme: ColorScheme) -> AttributedString {
        // Ranges are collected before mutating: `runs` is derived from the
        // string being edited, so iterating it while writing to it walks a
        // view that is being rebuilt underneath.
        let linkRanges = runs.compactMap { $0.link == nil ? nil : $0.range }
        guard !linkRanges.isEmpty else { return self }

        var styled = self
        for range in linkRanges {
            styled[range].foregroundColor = ground.linkColor(for: scheme)
            styled[range].underlineStyle = .single
        }
        return styled
    }
}
