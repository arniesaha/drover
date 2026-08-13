import Foundation
import Testing
@testable import DroverKit

private let now = Date(timeIntervalSince1970: 1_786_600_000)

// Both types are decode-only (they carry a custom `init(from:)`), so fixtures
// are built the way the app receives them — off the wire.
private func decode<T: Decodable>(_ type: T.Type, _ json: String) -> T {
    // swiftlint:disable:next force_try
    try! JSONDecoder().decode(type, from: Data(json.utf8))
}

private func coverage(tokenPercent: Double?) -> Coverage {
    let token = tokenPercent.map { "\($0)" } ?? "null"
    return decode(
        Coverage.self,
        """
        {"source": "drover_observed", "token_percent": \(token)}
        """
    )
}

private func metadata(
    ageSeconds: TimeInterval, freshness: AggregateFreshness = .fresh, tokenPercent: Double? = 71.4
) -> ObservedAggregateMetadata {
    let observedAt = ISO8601DateFormatter().string(from: now.addingTimeInterval(-ageSeconds))
    let token = tokenPercent.map { "\($0)" } ?? "null"
    return decode(
        ObservedAggregateMetadata.self,
        """
        {
          "source": "drover_observed",
          "observed_at": "\(observedAt)",
          "freshness": "\(freshness.rawValue)",
          "coverage": {"source": "drover_observed", "token_percent": \(token)}
        }
        """
    )
}

private func entry(
    _ key: String, sessions: Int, tokens: Int,
    metadata meta: ObservedAggregateMetadata? = nil
) -> DistributionPresentationBuilder.Entry {
    .init(key: key, sessionCount: sessions, totalTokens: tokens, metadata: meta)
}

private func build(
    _ entries: [DistributionPresentationBuilder.Entry],
    rank: DistributionRank = .sessions,
    section: ObservedAggregateMetadata? = nil,
    fallbackTokenPercent: Double? = 71.4
) -> DistributionSectionPresentation {
    DistributionPresentationBuilder.section(
        title: "Harnesses",
        singular: "harness",
        entries: entries,
        rank: rank,
        sectionMetadata: section,
        fallbackCoverage: coverage(tokenPercent: fallbackTokenPercent),
        now: now
    )
}

// MARK: - Rank

@Test func rankNamesWhatTheOtherTapWouldDo() {
    #expect(DistributionRank.sessions.toggleTitle == "By tokens")
    #expect(DistributionRank.tokens.toggleTitle == "By sessions")
    #expect(DistributionRank.sessions.other == .tokens)
}

@Test func sectionSaysWhatItIsRankedBy() {
    let section = build([entry("a", sessions: 2, tokens: 10)])

    // Ranking must be stated, not inferred from the order.
    #expect(section.subtitle == "1 harness · ranked by sessions")
}

@Test func sessionsAndTokensCanRankDifferently() {
    let entries = [
        entry("claude-code", sessions: 22, tokens: 167_310),
        entry("openclaw", sessions: 14, tokens: 23_832_216),
    ]

    let bySessions = build(entries, rank: .sessions)
    let byTokens = build(entries, rank: .tokens)

    // The whole point of the toggle: the leader changes.
    #expect(bySessions.rows[0].shareFraction == 1.0)
    #expect(byTokens.rows[1].shareFraction == 1.0)
    #expect(bySessions.rows[0].valueText == "22")
    #expect(byTokens.rows[0].valueText == "167,310")
}

// MARK: - Share bars

@Test func shareIsRelativeToTheSectionsLargestValue() {
    let section = build([
        entry("a", sessions: 20, tokens: 0),
        entry("b", sessions: 5, tokens: 0),
    ])

    #expect(section.rows[0].shareFraction == 1.0)
    #expect(section.rows[1].shareFraction == 0.25)
}

@Test func anAllZeroSectionDrawsNoFullBars() {
    let section = build([
        entry("a", sessions: 0, tokens: 0),
        entry("b", sessions: 0, tokens: 0),
    ])

    // Dividing by a zero maximum must not paint every row as the leader.
    #expect(section.rows.allSatisfy { $0.shareFraction == 0 })
}

@Test func everyRowPrintsItsNumberBesideTheBar() {
    let section = build([entry("a", sessions: 1_234, tokens: 0)])

    // A bar is never the only way to read a quantity.
    #expect(section.rows[0].valueText == "1,234")
}

// MARK: - Zero vs unknown

@Test func zeroTokensAcrossRealSessionsIsUnreportedNotZero() {
    let section = build(
        [entry("codex", sessions: 19, tokens: 0)],
        fallbackTokenPercent: 0
    )

    #expect(section.rows[0].tokensUnreported)
    #expect(section.rows[0].detailText == "tokens not reported")
}

@Test func aGenuineZeroStillPrintsZero() {
    // Coverage says tokens *were* reported, and they came to zero.
    let section = build(
        [entry("idle", sessions: 3, tokens: 0, metadata: metadata(ageSeconds: 0, tokenPercent: 100))]
    )

    #expect(!section.rows[0].tokensUnreported)
    #expect(section.rows[0].detailText == "0 tokens")
}

@Test func aSectionWithNoSessionsIsNotCalledUnreported() {
    let section = build([entry("empty", sessions: 0, tokens: 0)], fallbackTokenPercent: 0)

    // Nothing ran, so there is nothing missing.
    #expect(!section.rows[0].tokensUnreported)
}

@Test func reportedTokensReadNormally() {
    let section = build([entry("openclaw", sessions: 14, tokens: 23_832_216)])

    #expect(!section.rows[0].tokensUnreported)
    #expect(section.rows[0].detailText == "23,832,216 tokens")
}

// MARK: - Say it once

@Test func aRowMatchingItsSectionCarriesNoAge() {
    let shared = metadata(ageSeconds: 3 * 3_600)
    let section = build(
        [entry("a", sessions: 1, tokens: 5, metadata: shared)],
        section: shared
    )

    // The heading already said it; repeating it on every row is the bug.
    #expect(section.rows[0].ageText == nil)
}

@Test func aRowOlderThanItsSectionSpeaksUp() {
    let section = build(
        [entry("openclaw", sessions: 14, tokens: 5, metadata: metadata(ageSeconds: 4 * 86_400))],
        section: metadata(ageSeconds: 0)
    )

    #expect(section.rows[0].ageText == "4d")
}

@Test func aRowStalerThanItsSectionSpeaksUpEvenAtTheSameAge() {
    let age: TimeInterval = 3 * 3_600
    let section = build(
        [entry("a", sessions: 1, tokens: 5, metadata: metadata(ageSeconds: age, freshness: .stale))],
        section: metadata(ageSeconds: age, freshness: .fresh)
    )

    #expect(section.rows[0].ageText == "3h")
    #expect(section.rows[0].isStale)
}

@Test func rowsWithoutMetadataDoNotInventAnAge() {
    let section = build([entry("a", sessions: 1, tokens: 5)], section: metadata(ageSeconds: 0))

    #expect(section.rows[0].ageText == nil)
}

@Test func shortAgeUsesTheSameBucketsAsTheLongForm() {
    let f = DistributionPresentationBuilder.shortAge
    #expect(f(now, now) == "now")
    #expect(f(now.addingTimeInterval(-90), now) == "1m")
    #expect(f(now.addingTimeInterval(-3 * 3_600), now) == "3h")
    #expect(f(now.addingTimeInterval(-4 * 86_400), now) == "4d")
    #expect(f(nil, now) == nil)
}

// MARK: - Pluralisation

@Test func oneSessionIsNotOneSessions() {
    // The shipped screen printed "1 sessions" in three places.
    #expect(DistributionPresentationBuilder.pluralize(1, "session") == "session")
    #expect(DistributionPresentationBuilder.pluralize(0, "session") == "sessions")
    #expect(DistributionPresentationBuilder.pluralize(2, "session") == "sessions")
}

@Test func aSingleRowSectionReadsAsOne() {
    #expect(
        DistributionPresentationBuilder.subtitle(count: 1, singular: "harness", rank: .sessions)
            == "1 harness · ranked by sessions"
    )
    #expect(
        DistributionPresentationBuilder.subtitle(count: 5, singular: "harness", rank: .tokens)
            == "5 harnesses · ranked by tokens"
    )
}

// MARK: - Accessibility

@Test func glyphsNeverTravelAloneInTheAccessibilityLabel() {
    let section = build(
        [entry("openclaw", sessions: 1, tokens: 23_832_216, metadata: metadata(ageSeconds: 4 * 86_400, freshness: .stale))],
        section: metadata(ageSeconds: 0)
    )
    let label = section.rows[0].accessibilityLabel

    // The row shows a glyph and "4d"; VoiceOver still gets the full wording.
    #expect(label.contains("openclaw"))
    #expect(label.contains("1 session"))
    #expect(!label.contains("1 sessions"))
    #expect(label.contains("23,832,216 tokens"))
    #expect(label.contains("Updated 4d ago"))
    #expect(label.contains("Stale"))
}

@Test func unreportedTokensSaySoOutLoud() {
    let section = build([entry("codex", sessions: 19, tokens: 0)], fallbackTokenPercent: 0)

    #expect(section.rows[0].accessibilityLabel.contains("tokens not reported"))
}

// MARK: - Truncation keeps identity

@Test func projectKeysSplitOwnerFromRepo() {
    let split = ProjectKeySplit("arniesaha/drover")

    #expect(split.owner == "arniesaha")
    #expect(split.name == "drover")
}

@Test func aKeyWithNoOwnerIsAllName() {
    let split = ProjectKeySplit("drover")

    #expect(split.owner == nil)
    #expect(split.name == "drover")
}

@Test func nestedKeysKeepTheirTrailingName() {
    let split = ProjectKeySplit("github.com/arniesaha/drover")

    #expect(split.owner == "github.com/arniesaha")
    #expect(split.name == "drover")
}

@Test func aLeadingSlashIsNotAnOwner() {
    let split = ProjectKeySplit("/drover")

    #expect(split.owner == nil || split.name == "drover")
}
