import SwiftUI
import Testing
@testable import Drover
@testable import DroverKit

/// The rule these lock: a link is never drawn in the colour of the thing
/// behind it, and never marked by colour alone.
struct DroverLinkTests {
    private func parsed(_ markdown: String) -> AttributedString {
        DisplayBlock.parseInlineMarkdown(markdown)
    }

    private func linkRuns(_ string: AttributedString) -> [AttributedString.Runs.Element] {
        string.runs.filter { $0.link != nil }
    }

    // MARK: - The bug

    /// A URL sent from the composer lands in a bubble filled with the tint,
    /// and SwiftUI draws link runs in that same tint. Before the fix the URL
    /// was there, selectable, tappable — and the exact colour of its own
    /// background.
    @Test func aLinkIsNeverTheColourOfItsOwnGround() {
        for scheme in [ColorScheme.dark, .light] {
            let ground = DroverColor.accent.rgb(for: scheme)
            #expect(DroverLinkGround.accent.linkRGB(for: scheme) != ground,
                    "the user bubble's link is the bubble's own fill in \(scheme)")
        }
    }

    /// On the accent bubble the floor is the bubble's own prose, not a WCAG
    /// number: that prose is white on the tint (3.23:1 — below AA, and a
    /// pre-existing property of the bubble this change does not touch), so
    /// the contract a link owes is that it is never *less* readable than the
    /// sentence it appears in.
    @Test func urlsSentByTheUserAreAsReadableAsTheProseAroundThem() {
        for scheme in [ColorScheme.dark, .light] {
            let ground = DroverColor.accent.rgb(for: scheme)
            let link = contrast(DroverLinkGround.accent.linkRGB(for: scheme), ground)
            let prose = contrast(0xFF_FF_FF, ground)
            #expect(link >= prose,
                    "link is \(rounded(link)):1 against prose at \(rounded(prose)):1 in \(scheme)")
        }
    }

    /// Assistant prose sits on `surface`, so links there owe the same body
    /// floor `accentHi` already owes everywhere else.
    @Test func linksInAssistantProseClearTheBodyFloor() {
        for scheme in [ColorScheme.dark, .light] {
            let ratio = contrast(DroverLinkGround.surface.linkRGB(for: scheme),
                                 DroverColor.surface.rgb(for: scheme))
            #expect(ratio >= 4.5,
                    "link on surface in \(scheme) is \(rounded(ratio)):1")
        }
    }

    // MARK: - The transform

    @Test func everyLinkRunIsRecolouredAndUnderlined() {
        let styled = parsed("Opened https://github.com/a/b/pull/7 and https://example.com")
            .droverLinks(on: .surface, in: .dark)

        let links = linkRuns(styled)
        #expect(links.count == 2)
        for run in links {
            #expect(run.foregroundColor == DroverLinkGround.surface.linkColor(for: .dark))
            // Colour alone does not mark a link — the underline is what
            // survives both ramps, both grounds and colour-blind vision.
            #expect(run.underlineStyle == .single)
        }
    }

    @Test func theGroundDecidesTheColour() {
        let source = parsed("https://example.com")

        #expect(linkRuns(source.droverLinks(on: .accent, in: .dark)).first?.foregroundColor
                == DroverLinkGround.accent.linkColor(for: .dark))
        #expect(linkRuns(source.droverLinks(on: .surface, in: .dark)).first?.foregroundColor
                == DroverLinkGround.surface.linkColor(for: .dark))
    }

    /// A bare URL is the shape people actually paste, and it is the shape the
    /// markdown parser autolinks — so it is the shape that was invisible.
    @Test func bareURLsAreLinksAtAll() {
        #expect(linkRuns(parsed("https://github.com/arniesaha/drover/pull/42")).count == 1)
    }

    @Test func proseWithoutLinksIsUntouched() {
        let source = parsed("Just some **prose** with no URL in it")

        #expect(source.droverLinks(on: .accent, in: .dark) == source)
    }

    /// Emphasis, code spans and the rest of the inline parse must survive the
    /// pass — it edits link runs and nothing else.
    @Test func nonLinkStylingSurvives() {
        let styled = parsed("See **bold** at https://example.com now")
            .droverLinks(on: .surface, in: .dark)

        #expect(String(styled.characters) == "See bold at https://example.com now")
        #expect(styled.runs.contains { $0.inlinePresentationIntent == .stronglyEmphasized })
        #expect(styled.runs.filter { $0.underlineStyle != nil }.count == 1)
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
