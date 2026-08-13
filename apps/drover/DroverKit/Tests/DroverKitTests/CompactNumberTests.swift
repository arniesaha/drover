import Foundation
import Testing
@testable import DroverKit

@Test func theReportedTokenCountAbbreviates() {
    // The figure from the screenshot that prompted this.
    #expect(CompactNumber.abbreviated(63_132_964) == "63.1M")
}

@Test func smallNumbersAreLeftAlone() {
    #expect(CompactNumber.abbreviated(0) == "0")
    #expect(CompactNumber.abbreviated(240) == "240")
    #expect(CompactNumber.abbreviated(999) == "999")
}

@Test func thousandsAndMillionsGetOneDecimal() {
    #expect(CompactNumber.abbreviated(1_500) == "1.5K")
    #expect(CompactNumber.abbreviated(23_832_216) == "23.8M")
}

@Test func aWholeValueDropsItsTrailingZero() {
    // "1.0K" reads worse than "1K".
    #expect(CompactNumber.abbreviated(1_000) == "1K")
    #expect(CompactNumber.abbreviated(2_000_000) == "2M")
}

@Test func threeDigitMantissasDropTheDecimal() {
    // A tenth of six hundred million is noise at a glance.
    #expect(CompactNumber.abbreviated(631_000_000) == "631M")
    #expect(CompactNumber.abbreviated(150_000) == "150K")
}

@Test func billionsAreReached() {
    #expect(CompactNumber.abbreviated(1_200_000_000) == "1.2B")
}

@Test func boundariesLandOnTheLargerUnit() {
    #expect(CompactNumber.abbreviated(1_000_000) == "1M")
    #expect(CompactNumber.abbreviated(999_999) == "1000K")
}

@Test func negativesKeepTheirSign() {
    // Not expected from a token count, but a formatter that mangles them is
    // worse than one that does not.
    #expect(CompactNumber.abbreviated(-63_132_964) == "-63.1M")
    #expect(CompactNumber.abbreviated(-5) == "-5")
}

@Test func nothingPlausibleIsLongerThanSixCharacters() {
    // The triad puts three of these side by side; an unbounded one wraps.
    // Bounded to counts a fleet could actually report — this formats token
    // and session counts, not arbitrary integers, and `Int.max` would need a
    // suffix per three digits forever to stay short.
    for value in [0, 999, 1_500, 63_132_964, 631_000_000, 1_200_000_000, 500_000_000_000_000] {
        #expect(CompactNumber.abbreviated(value).count <= 6, "\(value)")
    }
}
