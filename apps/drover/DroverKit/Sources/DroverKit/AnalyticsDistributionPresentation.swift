import Foundation

// MARK: - DistributionRank

/// Which number a distribution list is ordered by.
///
/// Sessions and tokens disagree constantly — a harness with the most sessions
/// is routinely not the one burning the most tokens — so the screen names what
/// it is ranked by and offers the other with one tap, rather than picking one
/// and leaving the disagreement invisible.
public enum DistributionRank: String, Sendable, Equatable, CaseIterable {
    case sessions
    case tokens

    public var noun: String { rawValue }

    public var other: DistributionRank { self == .sessions ? .tokens : .sessions }

    /// Label for the control that switches ranking, e.g. "By tokens".
    public var toggleTitle: String { "By \(other.noun)" }
}

// MARK: - DistributionRowPresentation

/// One row of a distribution list, reduced to a name, the ranked number, a
/// share bar, and a single line of detail.
///
/// The shipped row printed three lines, two of which were identical on every
/// row in the section ("Drover observed · Updated 4d ago · Stale · 71.4% token
/// coverage"). Those facts now live in the section heading, and a row only
/// speaks up when it *differs* from its section — then it carries its own age
/// and nothing more.
public struct DistributionRowPresentation: Sendable, Equatable, Identifiable {
    public let id: String
    /// Display name. Never truncated in the middle — see `ProjectKeySplit`.
    public let title: String
    /// The ranked quantity, always printed beside the bar so the bar is never
    /// the only way to read it.
    public let valueText: String
    /// Share of the section's largest value, 0...1.
    public let shareFraction: Double
    /// The single detail line: token count, or the unreported notice.
    public let detailText: String
    /// Per-row extra that is *not* the same on every row — the harnesses and
    /// hosts that touched a project. The redesign collapses text that repeats
    /// identically down the list; this varies, so it stays.
    public let secondaryText: String?
    /// True when tokens were never reported, as opposed to genuinely zero.
    /// The row draws a hairline where the bar would be and prints an em dash.
    public let tokensUnreported: Bool
    /// Short age ("4d"), present only when this row's freshness differs from
    /// the section's. `nil` means "same as the heading already said".
    public let ageText: String?
    /// True when the row's freshness is anything but fresh, which pairs with
    /// `ageText` as a hollow ring.
    public let isStale: Bool
    /// The full, unabbreviated wording. The glyphs and the short age are
    /// shorthand for sighted scanning; this is what VoiceOver reads.
    public let accessibilityLabel: String

    public init(
        id: String,
        title: String,
        valueText: String,
        shareFraction: Double,
        detailText: String,
        secondaryText: String?,
        tokensUnreported: Bool,
        ageText: String?,
        isStale: Bool,
        accessibilityLabel: String
    ) {
        self.id = id
        self.title = title
        self.valueText = valueText
        self.shareFraction = shareFraction
        self.detailText = detailText
        self.secondaryText = secondaryText
        self.tokensUnreported = tokensUnreported
        self.ageText = ageText
        self.isStale = isStale
        self.accessibilityLabel = accessibilityLabel
    }
}

// MARK: - DistributionSectionPresentation

/// A whole distribution list: the facts that are true of every row, said once,
/// plus the rows that carry what varies.
public struct DistributionSectionPresentation: Sendable, Equatable {
    public let title: String
    /// "5 harnesses · ranked by sessions"
    public let subtitle: String
    public let rank: DistributionRank
    public let rows: [DistributionRowPresentation]

    public var isEmpty: Bool { rows.isEmpty }

    public init(
        title: String,
        subtitle: String,
        rank: DistributionRank,
        rows: [DistributionRowPresentation]
    ) {
        self.title = title
        self.subtitle = subtitle
        self.rank = rank
        self.rows = rows
    }
}

// MARK: - Building

public enum DistributionPresentationBuilder {
    /// One entry as the caller already has it, independent of which concrete
    /// model it came from (`ActivityBreakdown` and `ProjectActivity` differ in
    /// their key name and nothing else that matters here).
    public struct Entry: Sendable, Equatable {
        public let key: String
        public let sessionCount: Int
        public let totalTokens: Int
        public let metadata: ObservedAggregateMetadata?
        /// Varies per row, so it survives the collapse. Nil for dimensions
        /// where the row name already is the contributor.
        public let secondaryText: String?

        public init(
            key: String, sessionCount: Int, totalTokens: Int,
            metadata: ObservedAggregateMetadata?, secondaryText: String? = nil
        ) {
            self.key = key
            self.sessionCount = sessionCount
            self.totalTokens = totalTokens
            self.metadata = metadata
            self.secondaryText = secondaryText
        }
    }

    public static func section(
        title: String,
        singular: String,
        entries: [Entry],
        rank: DistributionRank,
        sectionMetadata: ObservedAggregateMetadata?,
        fallbackCoverage: Coverage,
        now: Date = .now
    ) -> DistributionSectionPresentation {
        let sectionAge = shortAge(sectionMetadata?.observedAt, now: now)
        let sectionFreshness = sectionMetadata?.freshness

        // Share is against the section's own largest value, so a bar means
        // "relative to the biggest thing here" and never implies a global
        // scale the screen has not shown.
        let largest = entries.map { rankedValue($0, rank: rank) }.max() ?? 0

        let rows = entries.map { entry -> DistributionRowPresentation in
            let unreported = tokensAreUnreported(
                entry: entry, fallbackCoverage: fallbackCoverage
            )
            let value = rankedValue(entry, rank: rank)
            let rowAge = shortAge(entry.metadata?.observedAt, now: now)
            let differs =
                entry.metadata != nil
                && (rowAge != sectionAge || entry.metadata?.freshness != sectionFreshness)
            let isStale = (entry.metadata?.freshness ?? .fresh) != .fresh

            return DistributionRowPresentation(
                id: entry.key,
                title: entry.key,
                valueText: number(value),
                // A section where everything is zero draws no bars at all
                // rather than five full ones.
                shareFraction: largest > 0 ? Double(value) / Double(largest) : 0,
                detailText: detail(entry: entry, unreported: unreported),
                secondaryText: entry.secondaryText,
                tokensUnreported: unreported,
                ageText: differs ? rowAge : nil,
                isStale: isStale,
                accessibilityLabel: accessibilityLabel(
                    entry: entry,
                    unreported: unreported,
                    metadata: entry.metadata,
                    fallbackCoverage: fallbackCoverage,
                    now: now
                )
            )
        }

        return DistributionSectionPresentation(
            title: title,
            subtitle: subtitle(count: entries.count, singular: singular, rank: rank),
            rank: rank,
            rows: rows
        )
    }

    static func subtitle(count: Int, singular: String, rank: DistributionRank) -> String {
        "\(count) \(pluralize(count, singular)) · ranked by \(rank.noun)"
    }

    static func rankedValue(_ entry: Entry, rank: DistributionRank) -> Int {
        rank == .sessions ? entry.sessionCount : entry.totalTokens
    }

    /// Zero tokens across sessions that definitely ran is not a measurement of
    /// zero, it is the absence of one — and printed as `0` it reads as a bug.
    /// Coverage is what distinguishes them: it exists precisely to say how much
    /// of the underlying data was attributable.
    static func tokensAreUnreported(entry: Entry, fallbackCoverage: Coverage) -> Bool {
        guard entry.totalTokens == 0, entry.sessionCount > 0 else { return false }
        let coverage = entry.metadata?.coverage ?? fallbackCoverage
        return (coverage.tokenPercent ?? 0) == 0
    }

    static func detail(entry: Entry, unreported: Bool) -> String {
        if unreported { return "tokens not reported" }
        return "\(number(entry.totalTokens)) tokens"
    }

    /// The full wording the shipped caption used, kept intact for VoiceOver
    /// even though the row now shows a glyph and a two-character age.
    static func accessibilityLabel(
        entry: Entry,
        unreported: Bool,
        metadata: ObservedAggregateMetadata?,
        fallbackCoverage: Coverage,
        now: Date
    ) -> String {
        let aggregate = ObservedAggregatePresentation(
            metadata: metadata, fallbackCoverage: fallbackCoverage, now: now
        )
        let sessions = "\(entry.sessionCount) \(pluralize(entry.sessionCount, "session"))"
        let tokens = unreported
            ? "tokens not reported"
            : "\(number(entry.totalTokens)) tokens"
        return [entry.key, sessions, tokens, entry.secondaryText, aggregate.freshnessText]
            .compactMap { $0 }
            .joined(separator: ", ")
    }

    /// "now" / "3m" / "3h" / "4d" — the same buckets the long form uses, so a
    /// row and the heading can never disagree about how old something is.
    static func shortAge(_ observedAt: Date?, now: Date) -> String? {
        guard let observedAt else { return nil }
        let seconds = max(0, now.timeIntervalSince(observedAt))
        if seconds < 60 { return "now" }
        if seconds < 3_600 { return "\(Int(seconds / 60))m" }
        if seconds < 86_400 { return "\(Int(seconds / 3_600))h" }
        return "\(Int(seconds / 86_400))d"
    }

    /// The shipped screen printed "1 sessions" wherever a count was 1.
    ///
    /// A bare `+ "s"` is not enough here: the words this screen actually
    /// pluralises include "harness", and "5 harnesss" is no better than
    /// "1 sessions". Sibilant endings take "es".
    public static func pluralize(_ count: Int, _ singular: String) -> String {
        guard count != 1 else { return singular }
        let sibilant = ["s", "x", "z", "ch", "sh"].contains { singular.hasSuffix($0) }
        return singular + (sibilant ? "es" : "s")
    }

    static func number(_ value: Int) -> String {
        let formatter = NumberFormatter()
        formatter.numberStyle = .decimal
        return formatter.string(from: NSNumber(value: value)) ?? String(value)
    }
}

// MARK: - ProjectKeySplit

/// Splits `owner/repo` so a long project key can wrap between its parts rather
/// than being ellipsed through the middle, which destroys exactly the half that
/// identifies it.
public struct ProjectKeySplit: Sendable, Equatable {
    public let owner: String?
    public let name: String

    public init(_ key: String) {
        guard let slash = key.lastIndex(of: "/"), slash != key.startIndex else {
            owner = nil
            name = key
            return
        }
        owner = String(key[key.startIndex..<slash])
        name = String(key[key.index(after: slash)...])
    }
}
