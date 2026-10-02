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
    static let insightFindingID = "0123456789abcdef0123456789abcdef"

    static func insightSummaryData() -> Data {
        jsonData(insightFinding())
    }

    static func insightDetailData() -> Data {
        jsonData([
            "finding": insightFinding(),
            "evidence": [
                [
                    "observed_at": "2026-08-08T18:01:00Z",
                    "source_ref": "fixture-source-a",
                    "fields": [
                        "host": "fixture-host",
                        "items": ["codex", 20],
                        "missing_value": NSNull(),
                    ] as [String: Any],
                    "excerpt": "Synthetic evidence for visual review.",
                ] as [String: Any],
                [
                    "observed_at": "2026-08-08T17:59:00Z",
                    "source_ref": "fixture-source-b",
                    "fields": ["attempt_count": 20] as [String: Any],
                    "excerpt": NSNull(),
                ] as [String: Any],
            ],
            "actions": ["check_again": ["available": true]],
        ])
    }

    private static func insightFinding() -> [String: Any] {
        [
            "finding_id": insightFindingID,
            "analyzer_id": "fixture-analyzer",
            "rule_id": "fixture-rule",
            "target_type": "hook",
            "target_id": "fixture-host/codex/pre-tool",
            "analyzer_class": "deterministic",
            "severity": "high",
            "confidence": "confirmed",
            "title": "Fixture hook needs attention",
            "impact": "The check cannot run until the hook is restored.",
            "remediation": [
                "Restore the hook from the checked-in configuration.",
                "Run Check Again to verify the restored hook.",
            ],
            "state": "open",
            "dismissal_reason": NSNull(),
            "first_seen_at": "2026-08-08T18:00:00Z",
            "last_seen_at": "2026-08-08T18:01:00Z",
            "resolved_at": NSNull(),
            "dismissed_at": NSNull(),
            "regressed_at": NSNull(),
        ]
    }

    // MARK: Capability envelopes (#420)

    /// Codex's schema v1 row exactly as a current host publishes it.
    private static var codexRow: [String: Any] { [
        "name": "codex", "enabled": true, "description": "Codex CLI", "command": [String](),
        "capabilities": [
            "schema_version": 1, "harness_id": "codex", "launch_modes": ["structured"],
            "approvals": false, "interrupt": true, "native_resume": true,
            "model_catalog": true, "usage": false, "worktree": true,
            "interactive_auth": true, "turn_preferences": true,
            "attachments": ["image/gif", "image/jpeg", "image/png", "image/webp"],
        ] as [String: Any],
    ] }

    /// A fixture adapter with a deliberately unusual mix: approvals but no
    /// interrupt, both launch modes, no model catalog or sign-in, and PNG-only
    /// attachments (the app sends JPEG, so attaching is unavailable).
    private static var labRow: [String: Any] { [
        "name": capabilityLabHarness, "enabled": true,
        "description": "Fixture adapter", "command": [String](),
        "capabilities": [
            "schema_version": 1, "harness_id": capabilityLabHarness,
            "launch_modes": ["pty", "structured"],
            "approvals": true, "interrupt": false, "native_resume": false,
            "model_catalog": false, "usage": false, "worktree": false,
            "interactive_auth": false, "turn_preferences": false,
            "attachments": ["image/png"],
        ] as [String: Any],
    ] }

    static let capabilityLabHarness = "fixture-lab"
    static let legacyHostID = "fixture-legacy-host"
    static let legacyHostName = "Legacy Mac"
    static let labSessionID = "fixture-lab-session"
    static let codexApprovalSessionID = "fixture-codex-approval"

    static func snapshotData() -> Data {
        jsonData([
            "hosts": [[
                "host_id": coreJourney.hostID,
                "status": "online",
                "connection_kind": "direct",
                "capabilities": [
                    "display_name": coreJourney.hostName,
                    "harnesses": [codexRow],
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

    /// The capability journey's fleet: the fixture host advertises Codex and
    /// the unusual fixture adapter; a second, pre-#418 host advertises no
    /// matrix at all and must offer nothing to launch.
    static func capabilitySnapshotData() -> Data {
        func session(_ id: String, harness: String) -> [String: Any] {
            [
                "session_id": id,
                "host_id": coreJourney.hostID,
                "harness": harness,
                "mode": "structured",
                "status": "working",
                "awaiting": "approval",
                "cwd": "/fixture/project",
                "preview": "Capability fixture: \(harness)",
                "last_activity": "2026-09-04T00:00:00Z",
            ]
        }
        return jsonData([
            "hosts": [
                [
                    "host_id": coreJourney.hostID,
                    "status": "online",
                    "connection_kind": "direct",
                    "capabilities": [
                        "display_name": coreJourney.hostName,
                        "harnesses": [codexRow, labRow],
                    ],
                ],
                [
                    "host_id": legacyHostID,
                    "status": "online",
                    "connection_kind": "direct",
                    "capabilities": [
                        "display_name": legacyHostName,
                        "harnesses": ["shell", ["name": "codex", "enabled": true]] as [Any],
                    ],
                ],
            ],
            "sessions": [
                session(labSessionID, harness: capabilityLabHarness),
                session(codexApprovalSessionID, harness: "codex"),
            ],
            "cwd_suggestions": [],
        ])
    }

    /// A snapshot that declares the cockpit available with insights enabled.
    ///
    /// `CockpitStore` gates every insight lifecycle action on `isCockpitAvailable`,
    /// which is false until a snapshot carrying `cockpit_api_version` arrives. A
    /// fixture that skips this renders the detail layout but cannot reach its own
    /// Check Again, Acknowledge or Dismiss actions.
    static func insightCapabilitySnapshotData() -> Data {
        jsonData([
            "hosts": [],
            "sessions": [],
            "cwd_suggestions": [],
            "cockpit_api_version": 1,
            "cockpit_sections": ["insights"],
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
        // Capability sessions wait on a tool approval, so the journey can
        // see who may answer it from iOS.
        if sessionID == labSessionID || sessionID == codexApprovalSessionID {
            messages.append([
                "event_id": "fixture-approval-\(sessionID)",
                "seq": 2,
                "type": "approval_prompt",
                "role": "system",
                "text": "approval needed: Bash",
                "payload": [
                    "request_id": "fixture-request",
                    "tool": "Bash",
                    "input": ["command": "ls"],
                ] as [String: Any],
            ])
        }
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
            "page_max_seq": messages.count,
            "max_seq": messages.count,
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
