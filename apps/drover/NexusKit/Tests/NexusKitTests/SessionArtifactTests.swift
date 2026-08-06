import Foundation
import Testing
@testable import NexusKit

@Suite struct SessionArtifactTests {
    private func action(_ seq: Int, command: String) -> HarnessMessage {
        HarnessMessage(seq: seq, type: .toolAction, text: "Bash",
                       payload: ["tool": .string("Bash"),
                                 "tool_use_id": .string("t\(seq)"),
                                 "input": .object(["command": .string(command)])])
    }

    private func result(_ seq: Int, text: String) -> HarnessMessage {
        HarnessMessage(seq: seq, type: .toolResult, text: text,
                       payload: ["tool_use_id": .string("t\(seq)")])
    }

    // MARK: - Branches

    @Test func branchComesFromThePushCommand() {
        let messages = [action(1, command: "git push --set-upstream origin drover/harness-8118c95d")]

        let artifacts = SessionArtifactExtractor.artifacts(in: messages)

        #expect(artifacts.count == 1)
        #expect(artifacts[0].kind == .branch)
        #expect(artifacts[0].value == "drover/harness-8118c95d")
        #expect(artifacts[0].action == "Copy")
    }

    @Test func chainedCommandsAreEachInspected() {
        let messages = [action(1, command: "git add -A && git commit -m wip && git push origin feature/x")]

        #expect(SessionArtifactExtractor.artifacts(in: messages).map(\.value) == ["feature/x"])
    }

    /// A branch that was only checked out was never published, so it is not
    /// an artifact — surfacing it would mean offering a link to nothing.
    @Test func nonPushGitCommandsYieldNothing() {
        let messages = [
            action(1, command: "git checkout -b drover/local-only"),
            action(2, command: "git status --short --branch"),
            action(3, command: "git log --oneline -n 5"),
        ]

        #expect(SessionArtifactExtractor.artifacts(in: messages).isEmpty)
    }

    @Test func refspecPushesReportTheRemoteBranch() {
        #expect(SessionArtifactExtractor.branch(inPushCommand: "git push origin local:remote-name")
                == "remote-name")
    }

    /// `HEAD`, flags and deletions name no branch a reader could act on.
    @Test func pushesWithoutANameableBranchAreIgnored() {
        for command in [
            "git push origin HEAD",
            "git push origin :old-branch",
            "git push origin --delete",
            "git push",
        ] {
            #expect(SessionArtifactExtractor.branch(inPushCommand: command) == nil, "\(command)")
        }
    }

    // MARK: - Pull requests

    @Test func pullRequestURLShortensToOwnerRepoAndNumber() {
        let messages = [result(1, text: "https://github.com/arniesaha/drover/pull/142")]

        let artifacts = SessionArtifactExtractor.artifacts(in: messages)

        #expect(artifacts.count == 1)
        #expect(artifacts[0].kind == .pullRequest)
        #expect(artifacts[0].value == "arniesaha/drover #142")
        #expect(artifacts[0].action == "Open")
        #expect(artifacts[0].url?.absoluteString == "https://github.com/arniesaha/drover/pull/142")
    }

    @Test func trailingPunctuationIsNotPartOfTheURL() {
        let messages = [
            HarnessMessage(seq: 1, type: .assistantOutput,
                           text: "Opened https://github.com/arniesaha/drover/pull/143."),
        ]

        #expect(SessionArtifactExtractor.artifacts(in: messages).first?.value == "arniesaha/drover #143")
    }

    /// A tool *action* holds the command about to run, not its outcome —
    /// reading URLs from it would announce a PR before it existed.
    @Test func urlsInsideACommandAreNotArtifacts() {
        let messages = [action(1, command: "gh pr view https://github.com/arniesaha/drover/pull/9")]

        #expect(SessionArtifactExtractor.artifacts(in: messages).isEmpty)
    }

    @Test func nonGitHubURLsAreIgnored() {
        let messages = [result(1, text: "See https://example.com/arniesaha/drover/pull/1")]

        #expect(SessionArtifactExtractor.artifacts(in: messages).isEmpty)
    }

    // MARK: - Collection behaviour

    @Test func repeatedMentionsProduceOneRow() {
        let messages = [
            action(1, command: "git push origin drover/x"),
            action(2, command: "git push --force-with-lease origin drover/x"),
            result(3, text: "https://github.com/a/b/pull/7"),
            HarnessMessage(seq: 4, type: .assistantOutput, text: "Opened https://github.com/a/b/pull/7"),
        ]

        let artifacts = SessionArtifactExtractor.artifacts(in: messages)

        #expect(artifacts.count == 2)
        #expect(artifacts.map(\.kind) == [.branch, .pullRequest])
    }

    @Test func aTranscriptWithNoArtifactsProducesNoRows() {
        let messages = [
            action(1, command: "pytest tests/ -q"),
            result(2, text: "40 passed in 35.61s"),
            HarnessMessage(seq: 3, type: .assistantOutput, text: "All green."),
        ]

        #expect(SessionArtifactExtractor.artifacts(in: messages).isEmpty)
    }
}
