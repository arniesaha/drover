import Foundation
import Testing
@testable import DroverKit

@Test func theReportedTokenCountAbbreviates() {
    // The figure from the screenshot that prompted this: 63M (>= 10, no decimals).
    #expect(CompactNumber.abbreviated(63_132_964) == "63M")
}

@Test func smallNumbersAreLeftAlone() {
    #expect(CompactNumber.abbreviated(0) == "0")
    #expect(CompactNumber.abbreviated(240) == "240")
    #expect(CompactNumber.abbreviated(999) == "999")
}

@Test func edgeCasesFollowTheContract() {
    #expect(CompactNumber.abbreviated(0) == "0")
    #expect(CompactNumber.abbreviated(999) == "999")
    #expect(CompactNumber.abbreviated(1_000) == "1K")
    #expect(CompactNumber.abbreviated(999_950) == "1M")
    #expect(CompactNumber.abbreviated(1_000_000) == "1M")
    #expect(CompactNumber.abbreviated(1_250_000_000) == "1.2B")
}

@Test func oneDecimalUnderTenZeroAbove() {
    #expect(CompactNumber.abbreviated(1_200_000) == "1.2M")
    #expect(CompactNumber.abbreviated(845_000) == "845K")
    #expect(CompactNumber.abbreviated(3_400_000_000) == "3.4B")
    #expect(CompactNumber.abbreviated(1_500) == "1.5K")
    #expect(CompactNumber.abbreviated(23_832_216) == "24M")
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
    #expect(CompactNumber.abbreviated(999_499) == "999K")
    #expect(CompactNumber.abbreviated(999_500) == "1M")
    #expect(CompactNumber.abbreviated(999_950) == "1M")
    #expect(CompactNumber.abbreviated(1_000_000) == "1M")
}

@Test func negativesKeepTheirSign() {
    #expect(CompactNumber.abbreviated(-63_132_964) == "-63M")
    #expect(CompactNumber.abbreviated(-5) == "-5")
}

@Test func exactValueUsesLocaleGrouping() {
    let us = Locale(identifier: "en_US")
    let de = Locale(identifier: "de_DE")
    #expect(CompactNumber.exact(1_250_000_000, locale: us) == "1,250,000,000")
    #expect(CompactNumber.exact(1_250_000_000, locale: de) == "1.250.000.000")
    #expect(CompactNumber.exact(0, locale: us) == "0")
    #expect(CompactNumber.exact(999, locale: us) == "999")
    #expect(CompactNumber.exact(1_000, locale: us) == "1,000")
}

@Test func abbreviationIsLocaleAware() {
    let de = Locale(identifier: "de_DE")
    #expect(CompactNumber.abbreviated(1_250_000_000, locale: de) == "1,2B")
    #expect(CompactNumber.abbreviated(1_500, locale: de) == "1,5K")
    #expect(CompactNumber.abbreviated(845_000, locale: de) == "845K")
}

@Test func nothingPlausibleIsLongerThanSixCharacters() {
    // The triad puts three of these side by side; an unbounded one wraps.
    for value in [0, 999, 1_500, 63_132_964, 631_000_000, 1_200_000_000, 500_000_000_000_000] {
        #expect(CompactNumber.abbreviated(value).count <= 6, "\(value)")
    }
}

