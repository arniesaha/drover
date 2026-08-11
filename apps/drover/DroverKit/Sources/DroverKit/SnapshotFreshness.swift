import Foundation

/// How much of the fleet snapshot is still worth believing.
///
/// `SessionStore.refresh()` deliberately keeps the last-known snapshot when a
/// refresh fails, which is right — a dropped link should not blank the inbox.
/// What was missing is the other half of that bargain: saying so. A card drawn
/// from a snapshot nobody could refresh describes what *was* true, and every
/// state-derived thing on it (the state phrase, the verb, the dot) is a claim
/// about now that we cannot support.
///
/// The provider capacity strip has always said this outright — "Showing last
/// reported values" — while session cards said nothing and quietly kept
/// counting up (#81). This is that same statement, in a form a card can use.
public struct SnapshotFreshness: Sendable, Equatable {
    /// How long a snapshot stays current with nothing landing on top of it.
    ///
    /// The inbox polls every 5s, so four intervals means several polls in a
    /// row have failed or never ran — a backgrounded app, a wedged first
    /// foreground poll, a link that dropped without anyone noticing yet. One
    /// missed poll is normal and must not flicker the whole list to stale.
    public static let tolerance: TimeInterval = 20

    /// When the last refresh *succeeded* — the snapshot's own timestamp, as
    /// distinct from any session's `lastActivity`, which is frozen inside it.
    public let lastUpdate: Date?

    /// Whether the most recent attempt reached the hub at all.
    public let isReachable: Bool

    public let now: Date

    /// What a caller with no store to ask gets: never stale, so every
    /// pre-existing call site keeps rendering exactly as it did.
    public static let live = SnapshotFreshness(lastUpdate: nil, isReachable: true, now: .distantPast)

    public init(lastUpdate: Date?, isReachable: Bool, now: Date = Date()) {
        self.lastUpdate = lastUpdate
        self.isReachable = isReachable
        self.now = now
    }

    /// How old the snapshot is, or nil if none has ever landed. Never
    /// negative: a hub clock running ahead is not a snapshot from the future.
    public var age: TimeInterval? {
        lastUpdate.map { max(0, now.timeIntervalSince($0)) }
    }

    /// Two ways in, and both matter. An unreachable hub is stale the moment we
    /// know it — the fleet line already says so, and cards must not sit beside
    /// that looking current. But a hub can also simply go quiet without any
    /// attempt reporting failure, which is what a backgrounded app comes back
    /// to; there the snapshot's age is the whole fact.
    public var isStale: Bool {
        if !isReachable { return true }
        guard let age else { return false }
        return age > Self.tolerance
    }

    /// The line a stale card shows where its verb used to be. Names the
    /// snapshot's age, because "how far behind am I" is the question the
    /// ticking `lastActivity` label was answering wrong.
    public var staleNote: String? {
        guard isStale else { return nil }
        guard let age else { return "Stale" }
        return "Stale · \(Self.ageText(age)) ago"
    }

    /// A session's activity age measured against the snapshot rather than
    /// against now, so a frozen card stops counting up. Nil while fresh — a
    /// live card keeps the ticking relative formatter it has always used.
    public func frozenActivityText(for activity: Date?) -> String? {
        guard isStale, let activity, let lastUpdate else { return nil }
        return "\(Self.ageText(max(0, lastUpdate.timeIntervalSince(activity)))) ago"
    }

    /// Compact and locale-free by design: this sits in the slot a verb used to
    /// occupy on a 393pt-wide card, beside a subtitle that is already
    /// truncating.
    public static func ageText(_ seconds: TimeInterval) -> String {
        let total = Int(max(0, seconds))
        switch total {
        case ..<60: return "\(total)s"
        case ..<3_600: return "\(total / 60)m"
        case ..<86_400: return "\(total / 3_600)h"
        default: return "\(total / 86_400)d"
        }
    }
}
