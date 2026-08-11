import Foundation
import Testing
@testable import DroverKit

// MARK: - Conversation cards

@Test func conversationCardMakesThePreviewTheLoudThing() {
    let session = SessionSummary(
        id: "s1",
        hostID: "mac-mini",
        harness: "codex",
        mode: "structured",
        status: "running",
        awaiting: "input",
        cwd: "/Volumes/M2 1/drover",
        lastActivity: nil,
        preview: "  Rework session cards for iPhone 17 Pro  "
    )

    let card = SessionCardPresentation(session: session, hostTitle: "Mac Mini")

    #expect(card.species == .conversation)
    #expect(card.kicker == "drover")
    #expect(card.title == "Rework session cards for iPhone 17 Pro")
    #expect(card.subtitle == "Codex · Mac Mini · asked a question")
    #expect(card.action == .answer)
    #expect(card.isTitlePlaceholder == false)
    #expect(card.sigil == nil)
}

/// A multi-line preview still has to yield a single loud line.
@Test func conversationCardTakesTheFirstMeaningfulPreviewLine() {
    let session = SessionSummary(
        id: "s1", hostID: "h", harness: "codex", mode: "structured",
        status: "running", awaiting: "approval", cwd: "/src/drover",
        lastActivity: nil,
        preview: "\n\n  Push the changes and open a PR  \nthen confirm the worktree is clean"
    )

    let card = SessionCardPresentation(session: session, hostTitle: "Mac Mini")

    #expect(card.title == "Push the changes and open a PR")
    #expect(card.action == .approve)
    #expect(card.subtitle == "Codex · Mac Mini · needs approval")
}

/// The shipping-build case: the hub sends no preview until the first
/// assistant text, so this is what most cards actually looked like. The old
/// behaviour filled the loudest line with "Codex session in drover" — a
/// string with no information in it. The state is the only real fact, so it
/// is promoted, and then *not* repeated in the subtitle.
@Test func conversationCardWithoutPreviewPromotesTheStateInsteadOfInventingATitle() {
    let session = SessionSummary(
        id: "s2", hostID: "mac-mini", harness: "claude-code", mode: "structured",
        status: "running", awaiting: "input",
        cwd: "/Users/arnabmac/max/projects/meridian",
        lastActivity: nil, preview: nil
    )

    let card = SessionCardPresentation(session: session, hostTitle: "Mac Mini")

    #expect(card.title == "Asked a question")
    #expect(card.isTitlePlaceholder == true)
    #expect(card.kicker == "meridian")
    #expect(card.subtitle == "Claude · Mac Mini", "the state must not be said twice")
}

@Test func finishedConversationOffersResume() {
    let session = SessionSummary(
        id: "s3", hostID: "h", harness: "codex", mode: "structured",
        status: "completed", awaiting: nil, cwd: "/src/drover",
        lastActivity: nil, preview: "Opened the pull request."
    )

    let card = SessionCardPresentation(session: session, hostTitle: "Mac Mini")

    #expect(card.action == .resume)
    #expect(card.subtitle == "Codex · Mac Mini · finished")
}

// MARK: - Terminal cards

@Test func terminalCardUsesTheLastOutputLineAndOffersAttach() {
    let session = SessionSummary(
        id: "p1", hostID: "mac-mini", harness: "shell", mode: "pty",
        status: "running", awaiting: nil,
        cwd: "/Users/arnabmac/src/nexus-shipper",
        lastActivity: nil,
        preview: "........................................ [100%]\n40 passed in 35.61s\n"
    )

    let card = SessionCardPresentation(session: session, hostTitle: "Mac Mini")

    #expect(card.species == .terminal)
    #expect(card.kicker == "~/src/nexus-shipper")
    #expect(card.title == "40 passed in 35.61s")
    #expect(card.action == .attach)
    #expect(card.sigil == "$")
    #expect(card.subtitle == "Shell · Mac Mini · attached")
}

/// With nothing printed yet the working directory is the loudest true thing,
/// so it becomes the title rather than being repeated as both kicker and
/// title.
@Test func terminalCardWithoutOutputPromotesThePathAndSaysSo() {
    let session = SessionSummary(
        id: "p2", hostID: "mac-mini", harness: "shell", mode: "pty",
        status: "running", awaiting: nil,
        cwd: "/Users/arnabmac/src/drover",
        lastActivity: nil, preview: nil
    )

    let card = SessionCardPresentation(session: session, hostTitle: "Mac Mini")

    #expect(card.title == "~/src/drover")
    #expect(card.kicker == nil)
    #expect(card.isTitlePlaceholder == true)
    #expect(card.subtitle == "Shell · Mac Mini · attached · no output yet")
}

@Test func exitedTerminalOffersReopenAndChangesItsSigil() {
    let session = SessionSummary(
        id: "p3", hostID: "h", harness: "shell", mode: "pty",
        status: "completed", awaiting: nil, cwd: "/home/arnab/src/drover",
        lastActivity: nil, preview: "40 passed in 35.61s"
    )

    let card = SessionCardPresentation(session: session, hostTitle: "nas")

    #expect(card.action == .reopen)
    #expect(card.sigil == "‹")
    #expect(card.kicker == "~/src/drover", "linux homes abbreviate too")
    #expect(card.subtitle == "Shell · nas · exited")
}

/// A terminal session can never be in the needs-you bucket, so it must never
/// produce an attention verb — this is the invariant that lets both species
/// share one list.
@Test func terminalCardsNeverOfferAnAttentionVerb() {
    for status in ["running", "completed", "errored", "terminated"] {
        let session = SessionSummary(
            id: "p", hostID: "h", harness: "shell", mode: "pty",
            status: status, awaiting: nil, cwd: nil, lastActivity: nil
        )
        let action = SessionCardPresentation(session: session, hostTitle: "h").action
        #expect(action == .attach || action == .reopen,
                "\(status) produced \(String(describing: action))")
    }
}

// MARK: - Species selection

/// `isStructured` treats a null mode as structured for every harness except
/// shell, so legacy sessions predating the field still pick the right card.
@Test func speciesFollowsTheSessionMode() {
    func species(mode: String?, harness: String) -> SessionCardPresentation.Species {
        SessionCardPresentation(
            session: SessionSummary(
                id: "s", hostID: "h", harness: harness, mode: mode,
                status: "running", awaiting: nil, cwd: nil, lastActivity: nil),
            hostTitle: "h"
        ).species
    }

    #expect(species(mode: "structured", harness: "codex") == .conversation)
    #expect(species(mode: "pty", harness: "shell") == .terminal)
    #expect(species(mode: nil, harness: "claude-code") == .conversation)
    #expect(species(mode: nil, harness: "shell") == .terminal)
}

// MARK: - Stale cards (#81)

/// The reported card. `awaiting: "input"` was true when the snapshot landed;
/// by the time it was read the session was back to work — and the card still
/// offered **Answer**, whose only effect would have been to push a turn into a
/// session mid-work. A verb is derived from `attention`, and `attention` is
/// exactly the field a stale snapshot cannot vouch for, so a stale card offers
/// no verb at all.
@Test func aStaleCardOffersNoVerbToActOn() {
    let session = SessionSummary(
        id: "openclaw", hostID: "nas", harness: "claude-code", mode: "structured",
        status: "running", awaiting: "input", cwd: "/home/arnab/src/openclaw",
        lastActivity: Date(timeIntervalSince1970: 1_754_913_600),
        preview: "Yeah go ahead"
    )

    let live = SessionCardPresentation(session: session, hostTitle: "NAS")
    let stale = SessionCardPresentation(
        session: session,
        hostTitle: "NAS",
        freshness: SnapshotFreshness(
            lastUpdate: Date(timeIntervalSince1970: 1_754_913_600),
            isReachable: false,
            now: Date(timeIntervalSince1970: 1_754_913_600 + 247)
        )
    )

    #expect(live.action == .answer)
    #expect(live.isStale == false)

    #expect(stale.isStale)
    #expect(stale.action == nil, "a stale card must not offer a verb derived from state we can't vouch for")
    #expect(stale.staleNote != nil)
    // The card still says what it last heard — it just stops pretending that
    // is current.
    #expect(stale.title == "Yeah go ahead")
    #expect(stale.subtitle == "Claude · NAS · asked a question")
}

/// Approve/deny and Watch go the same way as Answer: every verb on the card is
/// derived from `attention`.
@Test func everyVerbIsSuppressedWhileStaleNotJustAnswer() {
    let stale = SnapshotFreshness(lastUpdate: nil, isReachable: false, now: Date())

    for (awaiting, status, mode) in [
        ("approval", "running", "structured"),
        (nil, "running", "structured"),
        (nil, "running", "pty"),
        (nil, "completed", "structured"),
    ] as [(String?, String, String)] {
        let session = SessionSummary(
            id: "s", hostID: "h", harness: "codex", mode: mode,
            status: status, awaiting: awaiting, cwd: "/src/drover", lastActivity: nil
        )
        let card = SessionCardPresentation(session: session, hostTitle: "h", freshness: stale)
        #expect(card.action == nil, "\(mode)/\(status)/\(awaiting ?? "nil") still offered \(String(describing: card.action))")
    }
}

/// The specific deception (#81): `lastActivity` is frozen inside the stale
/// snapshot while the relative formatter recomputes against *now*, so a frozen
/// card counts up — "27 minutes ago", "28 minutes ago" — and reads as live.
/// A stale card measures activity against the snapshot it came from, so the
/// number stops moving, and the snapshot's own age is what is shown instead.
@Test func aStaleCardFreezesActivityAgainstTheSnapshotNotNow() throws {
    let snapshotTaken = Date(timeIntervalSince1970: 1_754_913_600)
    let session = SessionSummary(
        id: "openclaw", hostID: "nas", harness: "claude-code", mode: "structured",
        status: "running", awaiting: "input", cwd: "/home/arnab/src/openclaw",
        lastActivity: snapshotTaken.addingTimeInterval(-27 * 60),
        preview: "Yeah go ahead"
    )

    // Ten minutes after the last successful refresh, the card must still say
    // 27 minutes — not 37.
    let card = SessionCardPresentation(
        session: session,
        hostTitle: "NAS",
        freshness: SnapshotFreshness(
            lastUpdate: snapshotTaken, isReachable: false,
            now: snapshotTaken.addingTimeInterval(600)
        )
    )

    let frozen = try #require(card.frozenActivityText)
    #expect(frozen.contains("27m"), "activity was measured against now, not the snapshot: \(frozen)")
    let note = try #require(card.staleNote)
    #expect(note.contains("10m"), "the note must carry the snapshot's own age: \(note)")
}

/// A live card keeps rendering its timestamp with the ticking relative
/// formatter — the frozen string is a stale-only affordance.
@Test func aLiveCardDoesNotFreezeItsTimestamp() {
    let session = SessionSummary(
        id: "s", hostID: "h", harness: "codex", mode: "structured",
        status: "running", awaiting: nil, cwd: nil,
        lastActivity: Date(timeIntervalSince1970: 1_754_913_600)
    )

    let card = SessionCardPresentation(session: session, hostTitle: "h")

    #expect(card.frozenActivityText == nil)
    #expect(card.staleNote == nil)
    #expect(card.action == .watch)
}

@Test func sessionWithoutCwdStillProducesACard() {
    let session = SessionSummary(
        id: "s", hostID: "h", harness: "", mode: nil,
        status: "completed", awaiting: nil, cwd: nil, lastActivity: nil
    )

    let card = SessionCardPresentation(session: session, hostTitle: "Mac Mini")

    #expect(card.kicker == nil)
    #expect(card.title == "Finished")
    #expect(card.subtitle == "Session · Mac Mini")
}
