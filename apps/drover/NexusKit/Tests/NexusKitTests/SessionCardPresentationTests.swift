import Testing
@testable import NexusKit

@Test func sessionCardUsesPreviewAsPrimaryTitle() {
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

    let presentation = SessionCardPresentation(
        session: session,
        hostTitle: "Mac Mini"
    )

    #expect(presentation.title == "Rework session cards for iPhone 17 Pro")
    #expect(presentation.metadataText == "Codex · Waiting on you · Mac Mini · drover")
}

@Test func sessionCardFallsBackToHarnessAndProjectWhenPreviewIsMissing() {
    let session = SessionSummary(
        id: "s2",
        hostID: "mac-mini",
        harness: "claude-code",
        mode: "structured",
        status: "running",
        awaiting: nil,
        cwd: "/Users/arnabmac/max/projects/meridian",
        lastActivity: nil,
        preview: nil
    )

    let presentation = SessionCardPresentation(
        session: session,
        hostTitle: "Mac Mini"
    )

    #expect(presentation.title == "Claude session in meridian")
    #expect(presentation.metadataText == "Claude · Running · Mac Mini · meridian")
}

@Test func sessionCardUsesPlainSessionFallbackForEmptyHarnessAndProject() {
    let session = SessionSummary(
        id: "s3",
        hostID: "mac-mini",
        harness: "",
        mode: nil,
        status: "completed",
        awaiting: nil,
        cwd: nil,
        lastActivity: nil,
        preview: nil
    )

    let presentation = SessionCardPresentation(
        session: session,
        hostTitle: "Mac Mini"
    )

    #expect(presentation.title == "Session")
    #expect(presentation.metadataText == "Session · Exited · Mac Mini")
}
