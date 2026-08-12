import Foundation
import Testing
@testable import DroverKit

// Pure value type, no `MockURLProtocol` — safe at file scope, outside the
// `MockNetworkTests` serialized suite (see `ClientTests`' doc comment).

private let noon = Date(timeIntervalSince1970: 1_754_913_600)

@Test func aJustRefreshedSnapshotIsFresh() {
    let freshness = SnapshotFreshness(lastUpdate: noon, isReachable: true, now: noon.addingTimeInterval(3))

    #expect(freshness.isStale == false)
    #expect(freshness.staleNote == nil)
}

/// An unreachable hub is stale the moment we know it — the fleet line already
/// says so, and the cards must not keep claiming to be current beside it.
@Test func anUnreachableHubIsStaleImmediately() {
    let freshness = SnapshotFreshness(lastUpdate: noon, isReachable: false, now: noon.addingTimeInterval(1))

    #expect(freshness.isStale)
    #expect(freshness.age == 1)
}

/// The other way in: nothing reported a failure, but nothing refreshed either
/// (polling stopped while backgrounded, a wedged first foreground poll). The
/// snapshot's age is the fact, not whether anyone complained about it.
@Test func aSnapshotNobodyRefreshedGoesStaleOnItsOwn() {
    let quiet = SnapshotFreshness(lastUpdate: noon, isReachable: true, now: noon.addingTimeInterval(19))
    let old = SnapshotFreshness(lastUpdate: noon, isReachable: true, now: noon.addingTimeInterval(600))

    #expect(quiet.isStale == false, "a single missed poll is not staleness")
    #expect(old.isStale)
}

/// The note answers "how far behind am I", which is the question the ticking
/// `lastActivity` label was silently answering wrong.
@Test func theStaleNoteCarriesTheSnapshotsOwnAge() throws {
    let note = try #require(
        SnapshotFreshness(lastUpdate: noon, isReachable: false, now: noon.addingTimeInterval(247)).staleNote
    )

    #expect(note.contains("4m"), "note should name the snapshot age, got: \(note)")
    #expect(note.localizedCaseInsensitiveContains("stale"))
}

/// Nothing has ever landed, and we cannot reach the hub: still stale, and the
/// note must not invent an age it does not have.
@Test func aFreshnessWithNoSuccessfulRefreshStillReportsStale() throws {
    let freshness = SnapshotFreshness(lastUpdate: nil, isReachable: false, now: noon)

    #expect(freshness.isStale)
    #expect(freshness.age == nil)
    let note = try #require(freshness.staleNote)
    #expect(!note.contains("0"), "no age is not an age of zero: \(note)")
}

/// The default a caller with no store gets. Used by every presentation call
/// site that predates #81, so it has to mean "do not mark anything".
@Test func theLiveDefaultIsNeverStale() {
    #expect(SnapshotFreshness.live.isStale == false)
    #expect(SnapshotFreshness.live.staleNote == nil)
}

/// Ages are rendered compactly and without a locale, because they sit in the
/// slot a verb used to occupy on a 393pt-wide card.
@Test func ageTextIsCompactAcrossTheRange() {
    #expect(SnapshotFreshness.ageText(9) == "9s")
    #expect(SnapshotFreshness.ageText(65) == "1m")
    #expect(SnapshotFreshness.ageText(3 * 3600 + 61) == "3h")
    #expect(SnapshotFreshness.ageText(50 * 3600) == "2d")
    #expect(SnapshotFreshness.ageText(-5) == "0s", "a clock skew must not print a negative age")
}
