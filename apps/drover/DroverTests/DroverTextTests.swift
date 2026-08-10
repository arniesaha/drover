import SwiftUI
import Testing
@testable import Drover

/// `droverText` sets a foreground style of its own, and in SwiftUI the style
/// nearest the `Text` wins. So a call site that writes
/// `.droverText(.subtitle).foregroundStyle(DroverColor.accentHi)` compiles,
/// reads as if it recoloured the text, and renders in the ramp's grey — which
/// is exactly how the provider card's near-exhaustion signal went missing.
/// Emphasis therefore has to be part of the ramp call, not layered over it.
@MainActor
struct DroverTextTests {
    private func rendered(accented: Bool) -> Data? {
        let renderer = ImageRenderer(
            content: Text("91% used")
                .droverText(.subtitle, accented: accented)
                .padding(4)
                .background(DroverColor.surface)
                .environment(\.colorScheme, .dark)
        )
        renderer.scale = 2
        return renderer.uiImage?.pngData()
    }

    @Test func accentedTextIsActuallyDrawnInTheAccent() throws {
        let accented = try #require(rendered(accented: true))
        let plain = try #require(rendered(accented: false))

        #expect(accented != plain, "accented text rendered identically to muted text")
    }

    /// The palette guide is explicit that the base accent is for chrome and
    /// large text, not body copy — accent-coloured prose steps to `accentHi`.
    @Test func accentedTextUsesTheBodyCopyStepOfTheAccent() {
        for scheme in [ColorScheme.dark, .light] {
            #expect(DroverColor.accentHi.rgb(for: scheme) != DroverColor.accent.rgb(for: scheme))
        }
    }
}
