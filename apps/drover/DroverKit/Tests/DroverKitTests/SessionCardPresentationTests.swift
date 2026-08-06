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
        #expect([.attach, .reopen].contains(action), "\(status) produced \(action)")
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
