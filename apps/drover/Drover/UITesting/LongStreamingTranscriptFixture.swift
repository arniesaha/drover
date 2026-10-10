import Foundation
import DroverKit

/// Synthetic, deterministic wire data shared by previews and fixture launches.
/// Forty step groups (280 events), an approval, then a long answer delivered
/// as eighty chunks in one turn. No hub or credentials are needed.
enum LongStreamingTranscriptFixture {
    static let chunkInterval: Duration = .milliseconds(250)
    static var historyEvents: [[String: Any]] {
        var events: [[String: Any]] = []
        func append(_ type: String, _ text: String, _ payload: [String: Any] = [:]) {
            events.append(event(seq: events.count + 1, type: type, text: text, payload: payload))
        }
        for group in 1...40 {
            append("assistant_output", "Sample step group \(group): inspect the synthetic project.")
            for step in 1...3 {
                let toolID = "sample-tool-\(group)-\(step)"
                append("tool_action", "Read sample file", [
                    "tool": "Read", "tool_use_id": toolID,
                    "input": ["path": "/sample/project/file-\(step).txt"],
                ])
                append("tool_result", "Sample file contents verified.", ["tool_use_id": toolID])
            }
        }
        append("approval_prompt", "Approve the sample verification command", [
            "request_id": "sample-stream-approval", "tool": "Bash",
            "input": ["command": "printf 'sample verification'"],
        ])
        return events
    }

    static var streamingEvents: [[String: Any]] {
        let firstSeq = historyEvents.count + 1
        let chunks = (1...80).map { chunk in
            "Sample streaming answer, chunk \(chunk) of 80. " +
            "This synthetic paragraph exercises transcript wrapping and scrolling while new output arrives. " +
            "Sample User can inspect earlier step groups, return to the latest output, and review the pending approval.\n\n"
        }
        return chunks.enumerated().map { index, text in
            // The final event carries the complete answer as one tall bubble,
            // so the fixture exercises both incremental output and long rows.
            event(seq: firstSeq + index, type: "assistant_output",
                  text: index == chunks.count - 1 ? chunks.joined() : text,
                  payload: ["chunk_index": index + 1, "chunk_count": chunks.count])
        }
    }

    static func event(seq: Int, type: String, text: String, payload: [String: Any]) -> [String: Any] {
        ["event_id": "sample-stream-\(seq)", "seq": seq, "type": type,
         "role": type == "tool_result" ? "tool" : "assistant", "text": text,
         "turn_id": "sample-stream-turn", "payload": payload]
    }

    static func frame(_ event: [String: Any]) -> String {
        String(data: try! JSONSerialization.data(withJSONObject: event, options: [.sortedKeys]), encoding: .utf8)!
    }

    /// Implements the same bounded paging contract as the hub, including the
    /// fixed watermark used by cold open and older-history requests.
    static func historyData(url: URL) -> Data {
        let query = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
        func value(_ name: String) -> Int? { query.first { $0.name == name }?.value.flatMap(Int.init) }
        let limit = max(1, value("limit") ?? 50)
        let before = value("before_seq")
        let after = value("after_seq")
        let through = value("through_seq") ?? historyEvents.count
        let eligible = historyEvents.filter {
            let seq = $0["seq"] as! Int
            return seq <= through && (before == nil || seq < before!) && (after == nil || seq > after!)
        }
        let page = after == nil ? Array(eligible.suffix(limit)) : Array(eligible.prefix(limit))
        let minSeq = page.first?["seq"] as? Int
        let maxSeq = page.last?["seq"] as? Int
        return try! JSONSerialization.data(withJSONObject: [
            "messages": page, "max_seq": historyEvents.count,
            "page_min_seq": minSeq.map { $0 as Any } ?? NSNull(),
            "page_max_seq": maxSeq.map { $0 as Any } ?? NSNull(),
            "has_older": minSeq.map { $0 > 1 } ?? false,
            "has_newer": maxSeq.map { $0 < through } ?? false,
        ])
    }
}

#if DEBUG
import SwiftUI

#Preview("Long streaming transcript") {
    let scenario = UITestScenario(launchEnvironment: [
        "DROVER_UI_TEST_SCENARIO": "long-streaming",
        "DROVER_UI_TEST_RUN_ID": "00000000-0000-4000-8000-000000000080",
    ])!
    NavigationStack {
        ChatView(client: scenario.transport.client,
                 sessionID: FixtureScenarioData.primarySessionID,
                 harness: FixtureScenarioData.capabilityLabHarness,
                 recoveryStore: scenario.environment.chatRecoveryStore,
                 recoveryWriteGate: scenario.environment.chatRecoveryWriteGate,
                 recoveryGeneration: scenario.environment.chatRecoveryGeneration,
                 chatModelFactory: scenario.makeClient().chatModelFactory)
    }
}
#endif
