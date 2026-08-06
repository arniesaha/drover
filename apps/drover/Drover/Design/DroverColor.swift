import SwiftUI

/// The Nocturne palette contract: nine tokens, two ramps.
///
/// The whole theme lives here, so no view branches on `colorScheme` — a token
/// resolves its own ramp from the environment. Both ramps are Nocturne ramp
/// steps rather than new hues: light grounds one step down (neutral-200) so
/// `surface` cards read as lifted off `bg`, and the accent drops to
/// accent-600, the step that keeps outlined controls above 3:1 on a light
/// ground.
///
/// Two carve-outs deliberately do *not* resolve through these tokens:
///
///   - The terminal keeps a fixed dark ground in both appearances — a light
///     terminal fights every tool that writes ANSI colour (see `TerminalView`,
///     which already hardcodes its own background).
///   - Attention dots encode state by *form* (filled / hollow ring / faint
///     ring) rather than by hue, because a pale fill disappears on a light
///     ground. That is why there is exactly one accent here and no
///     per-state palette.
enum DroverColor {
    static let bg = PaletteToken(dark: 0x16_18_26, light: 0xE4_E7_F5)
    static let surface = PaletteToken(dark: 0x1D_1F_2B, light: 0xF3_F5_FE)
    static let sheet = PaletteToken(dark: 0x23_25_32, light: 0xF3_F5_FE)
    static let text = PaletteToken(dark: 0xE9_E9_ED, light: 0x29_2B_31)
    static let muted = PaletteToken(dark: 0xA4_A4_AB, light: 0x59_5D_6C)
    static let faint = PaletteToken(dark: 0x8D_8D_96, light: 0x75_79_8C)
    static let line = PaletteToken(dark: 0x2F_31_3B, light: 0xCF_D3_E5)
    static let accent = PaletteToken(dark: 0x91_84_D9, light: 0x79_6C_BF)
    /// The accent one step further from the ground (accent-400 dark,
    /// accent-700 light). The system guide is explicit that the base accent is
    /// tuned to only ~3:1 — "enough for icons, large text and interface
    /// chrome, not for body copy" — so accent-coloured *text*, live marks and
    /// active states use this step instead. `accentTextClearsBodyCopyFloor`
    /// is what stops the base accent creeping back into prose.
    static let accentHi = PaletteToken(dark: 0xB5_AB_FC, light: 0x5D_52_94)
    static let accentTint = PaletteToken(dark: 0x2B_27_41, light: 0xE7_E5_FE)

    /// The contract as data, in the design doc's order. Tests walk this so a
    /// token added here without a ramp — or with the two ramps swapped —
    /// fails the suite rather than shipping.
    static let all: [(name: String, token: PaletteToken)] = [
        ("bg", bg),
        ("surface", surface),
        ("sheet", sheet),
        ("text", text),
        ("muted", muted),
        ("faint", faint),
        ("line", line),
        ("accent", accent),
        ("accentHi", accentHi),
        ("accentTint", accentTint),
    ]
}

/// One palette entry: both ramps held as raw RGB so the table above reads as
/// the design doc's table, and so tests can assert contrast without a UI.
///
/// Conforms to `ShapeStyle` rather than vending a `Color`, which is what lets
/// call sites stay theme-blind: `.foregroundStyle(DroverColor.muted)` and
/// `.background(DroverColor.surface, in: shape)` resolve the right ramp
/// themselves. Use `color(for:)` only where an actual `Color` is unavoidable
/// (`.shadow(color:)` and the UIKit bridge).
struct PaletteToken: ShapeStyle, Equatable, Sendable {
    let dark: UInt32
    let light: UInt32

    func resolve(in environment: EnvironmentValues) -> Color {
        color(for: environment.colorScheme)
    }

    func rgb(for scheme: ColorScheme) -> UInt32 {
        scheme == .dark ? dark : light
    }

    func color(for scheme: ColorScheme) -> Color {
        Color(rgb: rgb(for: scheme))
    }
}

extension View {
    /// The one palette effect that can't be a per-view token: `.tint`
    /// propagates the accent into every system control (buttons, the send
    /// fill, switches) and is a `Color`, so somebody has to read the
    /// appearance. Doing it once at the composition root is what lets every
    /// other view stay theme-blind.
    func droverTint() -> some View {
        modifier(DroverTint())
    }
}

private struct DroverTint: ViewModifier {
    @Environment(\.colorScheme) private var colorScheme

    func body(content: Content) -> some View {
        content.tint(DroverColor.accent.color(for: colorScheme))
    }
}

extension Color {
    /// 0xRRGGBB. Opaque by construction — every Nocturne token is a solid
    /// ramp step, which is what makes the two ramps expressible as a table
    /// instead of alpha composited over an unknown ground.
    init(rgb: UInt32) {
        self.init(
            .sRGB,
            red: Double((rgb >> 16) & 0xFF) / 255,
            green: Double((rgb >> 8) & 0xFF) / 255,
            blue: Double(rgb & 0xFF) / 255,
            opacity: 1
        )
    }
}
