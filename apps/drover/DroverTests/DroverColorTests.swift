import SwiftUI
import Testing
@testable import Drover

/// Locks the palette *contract*, not the hex values themselves: every token
/// carries two distinct ramps, cards lift off the ground in both, and the
/// text ramps clear the contrast floors the design doc claims. Rebalancing a
/// ramp step is expected to keep these passing; breaking the contract is not.
struct DroverColorTests {
    // MARK: - Ramp selection

    @Test func eachTokenResolvesItsOwnRamp() {
        for (name, token) in DroverColor.all {
            #expect(token.rgb(for: .dark) == token.dark, "\(name) picked the wrong dark ramp")
            #expect(token.rgb(for: .light) == token.light, "\(name) picked the wrong light ramp")
        }
    }

    /// Guards the copy-paste failure this table invites: pasting one ramp into
    /// both columns compiles, renders, and silently makes a token theme-blind.
    @Test func everyTokenActuallyChangesBetweenRamps() {
        for (name, token) in DroverColor.all {
            #expect(token.dark != token.light, "\(name) is the same in both themes")
        }
    }

    // MARK: - Tide

    /// Tide retired the purple accent, and the hexes are not what is locked
    /// here — steps get rebalanced. What is locked is the hue *family*: an
    /// accent whose red channel leads is the old purple creeping back through
    /// a rebalance.
    @Test func theAccentIsTealInBothRamps() {
        let accents = [("accent", DroverColor.accent), ("accentHi", DroverColor.accentHi)]
        for scheme in [ColorScheme.dark, .light] {
            for (name, token) in accents {
                let rgb = token.rgb(for: scheme)
                let (red, green, blue) = channels(rgb)
                #expect(green > red && blue > red,
                        "\(name) in \(scheme) leads with red — that is the retired purple")
            }
        }
    }

    /// The other half of the swap: the grounds dropped their blue-violet cast
    /// so the teal is the only chromatic thing on screen. A ground that leans
    /// hard on one channel is décor competing with signal.
    @Test func theGroundsAreNearNeutral() {
        for scheme in [ColorScheme.dark, .light] {
            for (name, token) in [("bg", DroverColor.bg), ("surface", DroverColor.surface)] {
                let (red, green, blue) = channels(token.rgb(for: scheme))
                let spread = max(red, green, blue) - min(red, green, blue)
                #expect(spread <= 12,
                        "\(name) in \(scheme) spreads \(spread) across channels — it is tinted, not neutral")
            }
        }
    }

    private func channels(_ rgb: UInt32) -> (Int, Int, Int) {
        (Int((rgb >> 16) & 0xFF), Int((rgb >> 8) & 0xFF), Int(rgb & 0xFF))
    }

    // MARK: - The "cards read as lifted" rule

    /// Light mode grounds one step *down* (neutral-200) precisely so that
    /// neutral-100 cards sit above it. Dark does the same in the other
    /// direction. Either way `surface` must be lighter than `bg`, or cards
    /// stop reading as cards.
    @Test func surfaceLiftsOffTheGroundInBothThemes() {
        for scheme in [ColorScheme.dark, .light] {
            let ground = luminance(DroverColor.bg.rgb(for: scheme))
            let card = luminance(DroverColor.surface.rgb(for: scheme))
            #expect(card > ground, "surface does not lift off bg in \(scheme)")
        }
    }

    // MARK: - Contrast floors

    @Test func textRampsClearTheirContrastFloors() {
        // Body text is read at arm's length, so it gets an AAA-ish floor.
        // `muted` carries real sub-lines and gets AA. `faint` and `accent`
        // are meta and outlined-control territory — the doc's own claim is
        // 3:1, which is the large-text / non-text floor.
        let floors: [(name: String, token: PaletteToken, minimum: Double)] = [
            ("text", DroverColor.text, 7.0),
            ("muted", DroverColor.muted, 4.5),
            ("faint", DroverColor.faint, 3.0),
            ("accent", DroverColor.accent, 3.0),
        ]

        for scheme in [ColorScheme.dark, .light] {
            let ground = DroverColor.bg.rgb(for: scheme)
            for (name, token, minimum) in floors {
                let ratio = contrast(token.rgb(for: scheme), ground)
                #expect(ratio >= minimum,
                        "\(name) on bg in \(scheme) is \(rounded(ratio)):1, below \(minimum):1")
            }
        }
    }

    /// The system guide caps the base accent at "icons, large text and
    /// interface chrome, not body copy", and points accent-coloured prose at a
    /// deeper ramp step. `accentHi` is that step, so it owes the full AA text
    /// floor — this is the test that keeps the two accents from being used
    /// interchangeably.
    @Test func accentTextClearsBodyCopyFloor() {
        for scheme in [ColorScheme.dark, .light] {
            let ground = DroverColor.bg.rgb(for: scheme)
            let base = contrast(DroverColor.accent.rgb(for: scheme), ground)
            let text = contrast(DroverColor.accentHi.rgb(for: scheme), ground)

            #expect(text >= 4.5,
                    "accentHi on bg in \(scheme) is \(rounded(text)):1, below the 4.5:1 body floor")
            // The whole point of the second accent: it must actually be the
            // higher-contrast one, or the ramp step was picked on the wrong
            // side of the ground.
            #expect(text > base, "accentHi is no further from the ground than accent in \(scheme)")
        }
    }

    /// The accent tint is a *ground* for the decision block and step rows, so
    /// what matters is that body text stays readable on top of it.
    @Test func textStaysReadableOnTheAccentTint() {
        for scheme in [ColorScheme.dark, .light] {
            let ratio = contrast(DroverColor.text.rgb(for: scheme),
                                 DroverColor.accentTint.rgb(for: scheme))
            #expect(ratio >= 4.5,
                    "text on accentTint in \(scheme) is \(rounded(ratio)):1")
        }
    }

    // MARK: - WCAG helpers

    private func contrast(_ a: UInt32, _ b: UInt32) -> Double {
        let (la, lb) = (luminance(a), luminance(b))
        let (hi, lo) = (max(la, lb), min(la, lb))
        return (hi + 0.05) / (lo + 0.05)
    }

    private func luminance(_ rgb: UInt32) -> Double {
        func channel(_ shift: UInt32) -> Double {
            let value = Double((rgb >> shift) & 0xFF) / 255
            return value <= 0.03928 ? value / 12.92 : pow((value + 0.055) / 1.055, 2.4)
        }
        return 0.2126 * channel(16) + 0.7152 * channel(8) + 0.0722 * channel(0)
    }

    private func rounded(_ value: Double) -> String {
        String(format: "%.2f", value)
    }
}
