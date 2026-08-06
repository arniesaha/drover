import Foundation
import Testing
@testable import DroverKit

@Suite struct SessionEventSummaryTests {
    private func status(_ text: String, payload: [String: JSONValue] = [:]) -> HarnessMessage {
        HarnessMessage.fixture(seq: 1, type: .status, text: text, payload: payload)
    }

    @Test func singleEventShowsItsNameNotACount() {
        #expect(SessionEventSummary.title(for: [status("init")]) == "init")
    }

    @Test func multipleEventsShowACount() {
        let run = [status("hook_started"), status("hook_response"), status("init")]
        #expect(SessionEventSummary.title(for: run) == "3 session events")
    }

    @Test func namelessEventFallsBackToAGenericLabel() {
        #expect(SessionEventSummary.title(for: [status("")]) == "session event")
    }

    @Test func hookDetailShowsNameAndOutcome() {
        let message = status("hook_response", payload: [
            "hook_name": .string("SessionStart:startup"),
            "outcome": .string("success"),
        ])
        #expect(SessionEventSummary.detail(for: message)
                == "SessionStart:startup — success")
    }

    @Test func taskDetailShowsItsDescription() {
        let message = status("task_started", payload: [
            "description": .string("Phase 0 over Tailscale"),
        ])
        #expect(SessionEventSummary.detail(for: message)
                == "task_started — Phase 0 over Tailscale")
    }

    @Test func notificationDetailShowsSummaryAndState() {
        let message = status("task_notification", payload: [
            "summary": .string("Read NAS output"),
            "status": .string("completed"),
        ])
        #expect(SessionEventSummary.detail(for: message)
                == "task_notification — Read NAS output (completed)")
    }

    @Test func progressDetailShowsElapsedSeconds() {
        let message = status("tool_progress", payload: [
            "tool_name": .string("Bash"),
            "elapsed_time_seconds": .number(30),
        ])
        #expect(SessionEventSummary.detail(for: message) == "Bash running — 30s")
    }

    @Test func detailFallsBackToTheBareKind() {
        #expect(SessionEventSummary.detail(for: status("vcs_state_changed"))
                == "vcs_state_changed")
    }
}
