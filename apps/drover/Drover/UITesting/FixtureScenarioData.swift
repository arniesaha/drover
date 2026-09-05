import Foundation

/// Read-only, non-secret data shared by the DEBUG journey fixture and the
/// distribution demo. Transport behavior and mutable receipt state live in
/// DEBUG-only files so StoreRelease has no test controls to activate.
struct FixtureDemoScenario: Sendable, Equatable {
    struct Session: Sendable, Equatable {
        let id: String
        let title: String
        let harness: String
    }

    let serverURLString: String
    let hostID: String
    let hostName: String
    let credentialBindingID: UUID
    let sessions: [Session]
}

enum FixtureScenarioData {
    static let coreJourney = FixtureDemoScenario(
        serverURLString: "https://fixture.drover.invalid",
        hostID: "fixture-host",
        hostName: "Fixture Mac",
        credentialBindingID: UUID(uuidString: "00000000-0000-4000-8000-000000000042")!,
        sessions: [
            .init(id: "fixture-session", title: "Fixture core journey", harness: "codex"),
            .init(id: "fixture-launched-session", title: "Fixture launched journey", harness: "codex"),
            .init(id: "fixture-other-session", title: "Fixture other journey", harness: "codex"),
        ]
    )

    static let syntheticBearerToken = "fixture-token-no-secret"
    static let primarySessionID = "fixture-session"
    static let launchedSessionID = "fixture-launched-session"
    static let otherSessionID = "fixture-other-session"
    static let syntheticTurnID = "fixture-turn"

    static func snapshotData() -> Data {
        jsonData([
            "hosts": [[
                "host_id": coreJourney.hostID,
                "status": "online",
                "connection_kind": "direct",
                "capabilities": [
                    "display_name": coreJourney.hostName,
                    "harnesses": [["name": "codex", "enabled": true]],
                ],
            ]],
            "sessions": coreJourney.sessions.map { session in
                [
                    "session_id": session.id,
                    "host_id": coreJourney.hostID,
                    "harness": session.harness,
                    "mode": "structured",
                    "status": "working",
                    "cwd": "/fixture/project",
                    "preview": session.title,
                    "last_activity": "2026-09-04T00:00:00Z",
                ] as [String: Any]
            },
            "cwd_suggestions": [[
                "path": "/fixture/project",
                "source": "fixture",
                "host_id": coreJourney.hostID,
            ]],
        ])
    }

    static func historyData(sessionID: String, receiptTurnID: String?) -> Data {
        var messages: [[String: Any]] = [[
            "event_id": "fixture-intro-\(sessionID)",
            "seq": 1,
            "type": "assistant_output",
            "role": "assistant",
            "text": "Fixture ready: \(sessionID)",
            "payload": [:],
        ]]
        if let receiptTurnID {
            messages.append([
                "event_id": "fixture-receipt-\(receiptTurnID)",
                "seq": 2,
                "type": "user_input",
                "role": "user",
                "text": "Synthetic delivery.",
                "turn_id": receiptTurnID,
                "payload": [:],
            ])
        }
        return jsonData([
            "messages": messages,
            "page_min_seq": 1,
            "page_max_seq": receiptTurnID == nil ? 1 : 2,
            "max_seq": receiptTurnID == nil ? 1 : 2,
            "has_older": false,
            "has_newer": false,
        ])
    }

    static func modelCatalogData() -> Data {
        jsonData([
            "schema_version": 1,
            "host_id": coreJourney.hostID,
            "harness": "codex",
            "account_scope_id": NSNull(),
            "harness_version": NSNull(),
            "discovered_at": "2026-09-04T00:00:00Z",
            "stale": false,
            "stale_reason": NSNull(),
            "models": [],
        ])
    }

    private static func jsonData(_ object: Any) -> Data {
        // All values above are static, complete synthetic wire values. A
        // precondition here catches accidental edits before a demo or test can
        // silently emit malformed fixture data.
        guard JSONSerialization.isValidJSONObject(object) else {
            preconditionFailure("Fixture scenario data must be valid JSON")
        }
        return try! JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
    }
}
