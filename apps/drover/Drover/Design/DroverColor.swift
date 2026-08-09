import SwiftUI

/// The Tide palette contract: ten tokens, two ramps, one accent.
///
/// The whole theme lives here, so no view branches on `colorScheme` — a token
/// resolves its own ramp from the environment. Light grounds one step down
/// (neutral-200) so `surface` cards read as lifted off `bg`, and the accent
/// drops to tide-700, the step that keeps outlined controls above 3:1 on a
/// light ground.
///
/// Purple is retired: Tide is now *the* ramp rather than an alternative to
/// flip to. Two things moved with the hue. The grounds lost their blue-violet
/// cast — near-neutral charcoal dark, cool paper light — so the teal is the
/// only chromatic thing on screen and reads as signal rather than décor. And
/// the accent splits by job: `accent` carries chrome (icons, outlines, the
/// send disc) where 3:1 is the bar, while accent-coloured *text* steps to
/// `accentHi` — tide-400 dark, tide-800 light, 8.8:1 and 7.0:1 on their
/// grounds.
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
    static let bg = PaletteToken(dark: 0x15_16_1A, light: 0xE4_E7_E5)
    static let surface = PaletteToken(dark: 0x1D_1F_24, light: 0xF5_F7_F6)
    static let sheet = PaletteToken(dark: 0x23_26_2C, light: 0xF5_F7_F6)
    static let text = PaletteToken(dark: 0xE9_E9_EC, light: 0x2A_2B_2E)
    static let muted = PaletteToken(dark: 0xA4_A6_AD, light: 0x5B_5D_65)
    static let faint = PaletteToken(dark: 0x8C_8E_96, light: 0x76_78_81)
    static let line = PaletteToken(dark: 0x2F_32_3A, light: 0xD3_D6_D4)
    static let accent = PaletteToken(dark: 0x2A_A7_9C, light: 0x11_6E_68)
    /// The accent one step further from the ground (tide-400 dark, tide-800
    /// light). The system guide is explicit that the base accent is tuned for
    /// "icons, large text and interface chrome, not for body copy" — so
    /// accent-coloured *text*, live marks and active states use this step
    /// instead. `accentTextClearsBodyCopyFloor` is what stops the base accent
    /// creeping back into prose.
    static let accentHi = PaletteToken(dark: 0x4F_C7_BB, light: 0x0E_54_50)
    static let accentTint = PaletteToken(dark: 0x11_26_28, light: 0xD8_F0_EC)

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
