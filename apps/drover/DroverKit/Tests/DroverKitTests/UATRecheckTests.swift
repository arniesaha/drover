import Foundation
import Testing
import UserNotifications
@testable import DroverKit

struct UATRecheckTests {
    @Test @MainActor func launchErrorsUsePlainCopyWithoutPaths() {
        #expect(LaunchModel.errorMessage(for: DroverError.badRequest("cwd does not exist: /fixture/missing"))
                == "That folder no longer exists. Pick another folder.")
        #expect(LaunchModel.errorMessage(for: DroverError.badRequest("cwd is not a directory: /fixture/file"))
                == "That path is not a folder. Pick another folder.")
        #expect(!LaunchModel.errorMessage(for: DroverError.conflict("internal detail /fixture")).contains("/fixture"))
        #expect(LaunchModel.errorMessage(for: DroverError.transport("private detail"))
                == "Could not reach the host. Check the connection and try again.")
    }

    @Test func completionUsesFinishedCategoryAndNormalInterruption() {
        let completed = LocalNotifier.content(kind: .finished, id: "fixture")
        #expect(completed.categoryIdentifier == "finished")
        #expect(completed.body == "Finished: ready for review")
        #expect(completed.interruptionLevel == .active)
        let prompt = LocalNotifier.content(kind: .input, id: "fixture")
        #expect(prompt.categoryIdentifier == "input")
        #expect(prompt.interruptionLevel == .timeSensitive)
    }

    @Test func completedTurnIsNotAQuestionButApprovalStillIs() throws {
        var session = try JSONDecoder().decode(SessionSummary.self, from: Data("""
        {"session_id":"fixture","status":"running","mode":"structured",
         "awaiting":"input","notification_kind":"finished"}
        """.utf8))
        #expect(session.attention == .done)
        let card = SessionCardPresentation(session: session, hostTitle: "Fixture")
        #expect(card.action != .answer)
        session.awaiting = "approval"
        #expect(session.attention == .needsApproval)
        session.awaiting = "input"
        session.notificationKind = nil
        #expect(session.attention == .needsInput)
    }

    @Test func bannerSuppressionRequiresSameVisibleForegroundChat() {
        for foreground in [false, true] {
            for visible in [nil, "fixture", "other"] as [String?] {
                for session in [nil, "fixture", "other"] as [String?] {
                    #expect(NotificationVisibility.shouldSuppress(
                        sessionID: session, visibleSessionID: visible, isForeground: foreground
                    ) == (foreground && session != nil && session == visible))
                }
            }
        }
    }

    @Test @MainActor func departedChatCannotClearNewlyVisibleChat() {
        let visibility = NotificationVisibility()
        let parent = UUID(), child = UUID()
        visibility.isForeground = true
        visibility.show(sessionID: "parent", owner: parent)
        visibility.show(sessionID: "child", owner: child)
        visibility.hide(owner: parent)
        #expect(visibility.shouldSuppress(sessionID: "child"))
        #expect(!visibility.shouldSuppress(sessionID: "parent"))
        visibility.hide(owner: child)
        #expect(!visibility.shouldSuppress(sessionID: "child"))
    }

    @Test func staleHealthMustPersistAcrossSuccessfulFetches() throws {
        func health(_ state: String) throws -> ExporterHealth {
            try JSONDecoder().decode(ExporterHealth.self, from: Data("{\"state\":\"\(state)\"}".utf8))
        }
        var gate = ExporterFreshnessGate()
        #expect(gate.warningText == nil)
        gate.fetched(try health("lagging"))
        #expect(gate.warningText == nil)
        gate.fetched(nil)
        #expect(gate.warningText == nil)
        gate.fetched(try health("healthy"))
        #expect(gate.warningText == nil)
        gate.fetched(try health("stopped"))
        #expect(gate.warningText == nil)
        gate.fetched(try health("stalled"))
        #expect(gate.warningText != nil)
        gate.fetched(nil)
        #expect(gate.warningText != nil)
        gate.fetched(try health("healthy"))
        #expect(gate.warningText == nil)
    }
}
