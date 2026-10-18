import Foundation

public struct DemoHubResponse: Sendable {
    public let status: Int
    public let body: Data
    public let headers: [String: String]

    public init(status: Int, body: Data, headers: [String: String] = ["Content-Type": "application/json"]) {
        self.status = status
        self.body = body
        self.headers = headers
    }

    public static func json(status: Int, _ object: Any) -> DemoHubResponse {
        let body = (try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])) ?? Data()
        return DemoHubResponse(status: status, body: body, headers: ["Content-Type": "application/json"])
    }
}

/// The offline scripted fake hub powering App Review demo mode.
///
/// Contains purely fictional fleet data, simulated WebSocket streaming,
/// scripted approvals, diffs, artifacts, turn replies, interrupt/terminate,
/// quota card, 7d/30d analytics, history search, and virtual folder browsing.
///
/// Fully isolated: never opens network connections, never touches Keychain,
/// never touches production defaults or stored server credentials.
public final class DemoHub: @unchecked Sendable {
    public static let shared = DemoHub()

    private let lock = NSRecursiveLock()
    private var hosts: [[String: Any]] = []
    private var sessions: [[String: Any]] = []
    private var cwdSuggestions: [[String: Any]] = []
    private var messagesBySessionID: [String: [[String: Any]]] = [:]
    private var historyItems: [HistoryItem] = []
    private var streamContinuations: [String: [UUID: AsyncThrowingStream<String, Error>.Continuation]] = [:]

    public private(set) lazy var urlSession: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [DemoHubURLProtocol.self]
        return URLSession(configuration: config)
    }()

    public init() {
        reset()
    }

    /// Resets all demo state to pristine defaults.
    public func reset() {
        lock.withLock {
            hosts = DemoHubData.defaultHosts
            sessions = DemoHubData.defaultSessions
            cwdSuggestions = DemoHubData.defaultCwdSuggestions
            historyItems = DemoHubData.defaultHistoryItems
            messagesBySessionID = initialMessages()
            streamContinuations.removeAll()
        }
    }

    public func registerStreamContinuation(
        sessionID: String,
        id: UUID,
        continuation: AsyncThrowingStream<String, Error>.Continuation
    ) {
        lock.withLock {
            var map = streamContinuations[sessionID] ?? [:]
            map[id] = continuation
            streamContinuations[sessionID] = map
        }
    }

    public func unregisterStreamContinuation(sessionID: String, id: UUID) {
        lock.withLock {
            _ = streamContinuations[sessionID]?.removeValue(forKey: id)
        }
    }

    public func appendMessage(sessionID: String, message: [String: Any]) {
        lock.withLock {
            var list = messagesBySessionID[sessionID] ?? defaultMessages(for: sessionID)
            list.append(message)
            messagesBySessionID[sessionID] = list
            broadcastToStreamLocked(sessionID: sessionID, message: message)
        }
    }

    public func currentMessages(for sessionID: String) -> [[String: Any]] {
        lock.withLock {
            messagesBySessionID[sessionID] ?? defaultMessages(for: sessionID)
        }
    }

    public func harnessMessages(for sessionID: String) -> [HarnessMessage] {
        lock.withLock {
            let list = messagesBySessionID[sessionID] ?? defaultMessages(for: sessionID)
            guard let data = try? JSONSerialization.data(withJSONObject: list),
                  let msgs = try? JSONDecoder().decode([HarnessMessage].self, from: data) else {
                return []
            }
            return msgs
        }
    }

    public func snapshot() -> HarnessSnapshot {
        lock.withLock {
            let dict: [String: Any] = [
                "hosts": hosts,
                "sessions": sessions,
                "cwd_suggestions": cwdSuggestions,
                "cockpit_api_version": 1,
                "cockpit_sections": ["provider_capacity", "activity", "insights"],
            ]
            guard let data = try? JSONSerialization.data(withJSONObject: dict),
                  let snap = try? JSONDecoder().decode(HarnessSnapshot.self, from: data) else {
                preconditionFailure("Failed to decode demo snapshot")
            }
            return snap
        }
    }

    private func broadcastToStreamLocked(sessionID: String, message: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: message, options: [.sortedKeys]),
              let text = String(data: data, encoding: .utf8) else { return }
        for (_, continuation) in streamContinuations[sessionID] ?? [:] {
            continuation.yield(text)
        }
    }

    /// Factory for creating an offline `DroverClient` pointed at the fake hub.
    public func makeClient() -> DroverClient {
        guard let config = ServerConfig(urlString: DemoHubData.syntheticURLString) else {
            preconditionFailure("Invalid synthetic demo URL")
        }
        return DroverClient(
            config: config,
            token: DemoHubData.syntheticToken,
            credentialBindingID: UUID(uuidString: "00000000-0000-4000-8000-000000000099"),
            session: urlSession,
            retryGate: .shared,
            webSocketConnector: DemoHubWebSocketConnector()
        )
    }

    // MARK: - Initial Message Fixtures

    private func initialMessages() -> [String: [[String: Any]]] {
        var map: [String: [[String: Any]]] = [:]

        // 1. Streaming session
        map[DemoHubData.sessionStreaming] = [
            [
                "event_id": "stream-msg-1",
                "seq": 1,
                "type": "user_input",
                "role": "user",
                "text": "Please review and refactor the checkout payment validation flow.",
                "payload": [:] as [String: Any],
            ],
            [
                "event_id": "stream-msg-2",
                "seq": 2,
                "type": "assistant_output",
                "role": "assistant",
                "text": "I'll inspect the checkout validation handlers in `src/payment.ts`.",
                "payload": [:] as [String: Any],
            ],
        ]

        // 2. Approval session
        map[DemoHubData.sessionApproval] = [
            [
                "event_id": "appr-msg-1",
                "seq": 1,
                "type": "user_input",
                "role": "user",
                "text": "Run the pending database migration for user sessions table.",
                "payload": [:] as [String: Any],
            ],
            [
                "event_id": "appr-msg-2",
                "seq": 2,
                "type": "assistant_output",
                "role": "assistant",
                "text": "I located `migrations/004_sessions.sql`. This migration creates the `user_sessions` table and required index. Requesting operator permission to execute `psql`.",
                "payload": [:] as [String: Any],
            ],
            [
                "event_id": "appr-msg-3",
                "seq": 3,
                "type": "approval_prompt",
                "role": "assistant",
                "text": "Run database migration",
                "payload": [
                    "request_id": DemoHubData.approvalRequestID,
                    "tool_name": "psql",
                    "input": "psql -h localhost -d acme_db -f migrations/004_sessions.sql",
                ] as [String: Any],
            ],
        ]

        // 3. Diff & Artifacts session
        map[DemoHubData.sessionDiffArtifacts] = [
            [
                "event_id": "diff-msg-1",
                "seq": 1,
                "type": "user_input",
                "role": "user",
                "text": "Update NavigationBar to support dynamic responsive tabs and push to origin.",
                "payload": [:] as [String: Any],
            ],
            [
                "event_id": "diff-msg-2",
                "seq": 2,
                "type": "assistant_output",
                "role": "assistant",
                "text": "I'll update `src/components/Navigation.tsx` with responsive tabs and push a feature branch.",
                "payload": [:] as [String: Any],
            ],
            [
                "event_id": "diff-msg-3",
                "seq": 3,
                "type": "tool_action",
                "role": "assistant",
                "text": "Edit src/components/Navigation.tsx",
                "payload": [
                    "tool_name": "Edit",
                    "file_path": "src/components/Navigation.tsx",
                    "old_string": "export function Navigation() {\n  return <nav className=\"tabs\">Classic Tabs</nav>;\n}",
                    "new_string": "export function Navigation() {\n  return (\n    <nav className=\"tabs-responsive\">\n      <Tabs scrollable activeTab={active}>\n        <Tab label=\"Overview\" />\n        <Tab label=\"Sessions\" />\n        <Tab label=\"Analytics\" />\n      </Tabs>\n    </nav>\n  );\n}",
                ] as [String: Any],
            ],
            [
                "event_id": "diff-msg-4",
                "seq": 4,
                "type": "tool_action",
                "role": "assistant",
                "text": "Push branch to origin",
                "payload": [
                    "tool_name": "Bash",
                    "input": [
                        "command": "git push -u origin feat/responsive-nav",
                    ],
                ] as [String: Any],
            ],
            [
                "event_id": "diff-msg-5",
                "seq": 5,
                "type": "tool_result",
                "role": "tool",
                "text": "To github.com:acme/web-app.git\n * [new branch] feat/responsive-nav -> feat/responsive-nav\nbranch 'feat/responsive-nav' set up to track 'origin/feat/responsive-nav'.",
                "payload": [:] as [String: Any],
            ],
            [
                "event_id": "diff-msg-6",
                "seq": 6,
                "type": "assistant_output",
                "role": "assistant",
                "text": "Branch pushed and pull request opened: https://github.com/acme/web-app/pull/142\nAll changes are staged and ready for review.",
                "payload": [:] as [String: Any],
            ],
        ]

        // 4. Scripted Reply session
        map[DemoHubData.sessionReply] = [
            [
                "event_id": "reply-msg-1",
                "seq": 1,
                "type": "user_input",
                "role": "user",
                "text": "Can you add a JSON format option to drover-cli inspect?",
                "payload": [:] as [String: Any],
            ],
            [
                "event_id": "reply-msg-2",
                "seq": 2,
                "type": "assistant_output",
                "role": "assistant",
                "text": "I've added the `--json` option to the CLI flags parser in `src/cli/inspect.rs`.\n\nThe JSON serializer is ready. Would you like me to add benchmark tests for large outputs?",
                "payload": [:] as [String: Any],
            ],
        ]

        // 5. Interrupt & Terminate session
        map[DemoHubData.sessionInterrupt] = [
            [
                "event_id": "intr-msg-1",
                "seq": 1,
                "type": "user_input",
                "role": "user",
                "text": "Run the full integration test suite.",
                "payload": [:] as [String: Any],
            ],
            [
                "event_id": "intr-msg-2",
                "seq": 2,
                "type": "assistant_output",
                "role": "assistant",
                "text": "Starting cargo test runner across all integration crates...",
                "payload": [:] as [String: Any],
            ],
            [
                "event_id": "intr-msg-3",
                "seq": 3,
                "type": "tool_action",
                "role": "assistant",
                "text": "Running test suite",
                "payload": [
                    "tool_name": "run_command",
                    "command": "cargo test --release --all-targets -- --nocapture",
                ] as [String: Any],
            ],
        ]

        return map
    }

    // MARK: - Request Routing

    public func handleRequest(_ request: URLRequest) -> DemoHubResponse {
        guard let url = request.url else {
            return .json(status: 400, ["error": "missing url"])
        }
        guard url.host == DemoHubData.syntheticHost else {
            return .json(status: 403, ["error": "demo hub rejects non-synthetic host"])
        }

        let method = request.httpMethod ?? "GET"
        let path = url.path

        return lock.withLock {
            switch (method, path) {
            case ("GET", "/readyz"), ("GET", "/healthz"):
                return .json(status: 200, ["status": "ok", "version": "0.1.0-demo"])

            case ("GET", "/harness"):
                return .json(status: 200, [
                    "hosts": hosts,
                    "sessions": sessions,
                    "cwd_suggestions": cwdSuggestions,
                    "cockpit_api_version": 1,
                    "cockpit_sections": ["provider_capacity", "activity", "insights"],
                ])

            case ("GET", "/cockpit/overview"):
                return .json(status: 200, DemoHubData.cockpitOverview(sessions: sessions))

            case ("GET", "/analytics"):
                let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
                let days = items.first(where: { $0.name == "days" })?.value.flatMap(Int.init) ?? 7
                let metric = items.first(where: { $0.name == "metric" })?.value
                return .json(status: 200, DemoHubData.analytics(days: days, metric: metric))

            case ("GET", "/insights"):
                return .json(status: 200, DemoHubData.defaultInsights)

            case ("GET", let p) where p.hasPrefix("/insights/"):
                return .json(status: 200, [
                    "finding": (DemoHubData.defaultInsights["findings"] as? [[String: Any]])?.first ?? [:],
                    "evidence": [
                        [
                            "observed_at": "2026-10-17T22:00:00Z",
                            "source_ref": "lockfile-check",
                            "fields": ["mismatched_packages": 3] as [String: Any],
                            "excerpt": "package-lock.json integrity hash mismatch for express@4.19.2",
                        ] as [String: Any]
                    ],
                    "actions": ["check_again": ["available": true]],
                ])

            case ("GET", "/work/list"):
                return .json(status: 200, ["items": [] as [Any]])

            case ("GET", "/sessions/history/facets"):
                return .json(status: 200, DemoHubData.defaultFacets)

            case ("GET", "/sessions/history"):
                return handleHistoryRequest(url: url)

            case ("GET", let p) where p.hasPrefix("/harness/hosts/") && p.hasSuffix("/fs/list"):
                let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
                let fsPath = items.first(where: { $0.name == "path" })?.value ?? ""
                return .json(status: 200, DemoHubData.directoryListing(path: fsPath))

            case ("GET", let p) where p.hasPrefix("/harness/hosts/") && p.hasSuffix("/model-catalog"):
                return .json(status: 200, DemoHubData.defaultModelCatalog)

            case ("POST", let p) where p.hasPrefix("/harness/hosts/") && p.hasSuffix("/sessions"):
                return handleLaunchSession(request: request, url: url)

            case ("GET", let p) where p.hasPrefix("/harness/sessions/") && p.hasSuffix("/messages"):
                let sessionID = p
                    .replacingOccurrences(of: "/harness/sessions/", with: "")
                    .replacingOccurrences(of: "/messages", with: "")
                return handleGetMessages(sessionID: sessionID, url: url)

            case ("POST", let p) where p.hasPrefix("/harness/sessions/") && p.hasSuffix("/turns"):
                let sessionID = p
                    .replacingOccurrences(of: "/harness/sessions/", with: "")
                    .replacingOccurrences(of: "/turns", with: "")
                return handleTurnSubmission(sessionID: sessionID, request: request)

            case ("POST", let p) where p.hasPrefix("/harness/sessions/") && p.hasSuffix("/permission"):
                let sessionID = p
                    .replacingOccurrences(of: "/harness/sessions/", with: "")
                    .replacingOccurrences(of: "/permission", with: "")
                return handlePermissionDecision(sessionID: sessionID, request: request)

            case ("POST", let p) where p.hasPrefix("/harness/sessions/") && p.hasSuffix("/interrupt"):
                let sessionID = p
                    .replacingOccurrences(of: "/harness/sessions/", with: "")
                    .replacingOccurrences(of: "/interrupt", with: "")
                return handleInterrupt(sessionID: sessionID)

            case ("POST", let p) where p.hasPrefix("/harness/sessions/") && p.hasSuffix("/terminate"):
                let sessionID = p
                    .replacingOccurrences(of: "/harness/sessions/", with: "")
                    .replacingOccurrences(of: "/terminate", with: "")
                return handleTerminate(sessionID: sessionID)

            case ("GET", "/auth/device/notifications/mode"), ("PUT", "/auth/device/notifications/mode"):
                return .json(status: 200, ["mode": "immediate"])

            case ("GET", "/auth/device/content-analysis"), ("PUT", "/auth/device/content-analysis"):
                return .json(status: 200, ["enabled": false])

            case ("POST", "/auth/device/token"):
                return .json(status: 200, ["status": "ok"])

            case ("GET", let p) where p.hasPrefix("/harness/hosts/") && p.hasSuffix("/exporter-health"):
                return .json(status: 200, ["status": "ok", "timestamp": "2026-10-17T22:00:00Z"])

            default:
                return .json(status: 404, ["error": "not found on demo hub"])
            }
        }
    }

    // MARK: - Handlers

    private func handleHistoryRequest(url: URL) -> DemoHubResponse {
        let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
        let query = items.first(where: { $0.name == "q" || $0.name == "query" })?.value?.lowercased() ?? ""
        let stateFilters = Set(items.filter { $0.name == "state" }.compactMap(\.value))
        let hostFilters = Set(items.filter { $0.name == "host" }.compactMap(\.value))
        let harnessFilters = Set(items.filter { $0.name == "harness" }.compactMap(\.value))
        let repoFilters = Set(items.filter { $0.name == "repo" }.compactMap(\.value))

        var filtered = historyItems

        if !query.isEmpty {
            filtered = filtered.filter { item in
                item.title.lowercased().contains(query)
                    || (item.summary?.lowercased().contains(query) ?? false)
                    || (item.repo?.lowercased().contains(query) ?? false)
            }
        }
        if !stateFilters.isEmpty {
            filtered = filtered.filter { stateFilters.contains($0.state.rawValue) }
        }
        if !hostFilters.isEmpty {
            filtered = filtered.filter { hostFilters.contains($0.host.id) }
        }
        if !harnessFilters.isEmpty {
            filtered = filtered.filter { harnessFilters.contains($0.harness) }
        }
        if !repoFilters.isEmpty {
            filtered = filtered.filter { item in
                guard let repo = item.repo else { return false }
                return repoFilters.contains(repo)
            }
        }

        let encodedItems = filtered.map { item -> [String: Any] in
            var dict: [String: Any] = [
                "id": item.id,
                "title": item.title,
                "harness": item.harness,
                "host": [
                    "id": item.host.id,
                    "name": item.host.name,
                    "retired": item.host.retired,
                ] as [String: Any],
                "state": item.state.rawValue,
                "has_transcript": item.hasTranscript,
            ]
            if let model = item.model { dict["model"] = model }
            if let repo = item.repo { dict["repo"] = repo }
            if let branch = item.branch { dict["branch"] = branch }
            if let status = item.status { dict["status"] = status }
            if let summary = item.summary { dict["summary"] = summary }
            if let startedAt = item.startedAt {
                dict["started_at"] = ISO8601DateFormatter().string(from: startedAt)
            }
            if let endedAt = item.endedAt {
                dict["ended_at"] = ISO8601DateFormatter().string(from: endedAt)
            }
            if let lastActivity = item.lastActivity {
                dict["last_activity"] = ISO8601DateFormatter().string(from: lastActivity)
            }
            if let tokens = item.tokens {
                dict["tokens"] = [
                    "input": tokens.input as Any,
                    "output": tokens.output as Any,
                    "cache_read": tokens.cacheRead as Any,
                    "cache_write": tokens.cacheWrite as Any,
                ] as [String: Any]
            }
            return dict
        }

        return .json(status: 200, [
            "items": encodedItems,
            "next_cursor": NSNull(),
            "has_more": false,
            "page_size": filtered.count,
            "truncated": false,
            "skipped_rows": 0,
        ])
    }

    private func handleLaunchSession(request: URLRequest, url: URL) -> DemoHubResponse {
        var promptText = "New Demo Session"
        var cwdPath = "/projects/acme-web-app"
        var hostID = DemoHubData.hostStudio

        if let body = request.bodyData(),
           let payload = try? JSONSerialization.jsonObject(with: body) as? [String: Any] {
            if let p = payload["prompt"] as? String, !p.isEmpty { promptText = p }
            if let c = payload["cwd"] as? String, !c.isEmpty { cwdPath = c }
        }

        let pathComponents = url.pathComponents
        if let hostIdx = pathComponents.firstIndex(of: "hosts"), hostIdx + 1 < pathComponents.count {
            hostID = pathComponents[hostIdx + 1]
        }

        let newSessionID = DemoHubData.sessionLaunched
        let now = ISO8601DateFormatter().string(from: Date())

        let newSession: [String: Any] = [
            "session_id": newSessionID,
            "host_id": hostID,
            "host_display_name": hostID == DemoHubData.hostWorkstation ? "Linux Workstation" : "Mac Studio",
            "harness": "claude-code",
            "mode": "structured",
            "status": "working",
            "awaiting": NSNull(),
            "cwd": cwdPath,
            "repo_name": cwdPath.components(separatedBy: "/").last ?? "demo-project",
            "preview": promptText,
            "last_activity": now,
            "started_at": now,
            "model": "claude-3-7-sonnet",
        ]

        sessions.insert(newSession, at: 0)

        messagesBySessionID[newSessionID] = [
            [
                "event_id": "launch-msg-1",
                "seq": 1,
                "type": "user_input",
                "role": "user",
                "text": promptText,
                "payload": [:] as [String: Any],
            ],
            [
                "event_id": "launch-msg-2",
                "seq": 2,
                "type": "assistant_output",
                "role": "assistant",
                "text": "Started session in `\(cwdPath)`. Analyzing project workspace and requirements.",
                "payload": [:] as [String: Any],
            ],
        ]

        return .json(status: 201, [
            "session_id": newSessionID,
            "mode": "structured",
        ])
    }

    private func handleGetMessages(sessionID: String, url: URL) -> DemoHubResponse {
        let allMsgs = messagesBySessionID[sessionID] ?? defaultMessages(for: sessionID)
        let queryItems = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
        let queryMap = Dictionary(queryItems.compactMap { item in
            item.value.map { (item.name, $0) }
        }, uniquingKeysWith: { _, last in last })

        let limit = queryMap["limit"].flatMap(Int.init) ?? 50
        var filtered = allMsgs

        if let afterStr = queryMap["after_seq"], let afterSeq = Int(afterStr) {
            filtered = filtered.filter { (($0["seq"] as? Int) ?? 0) > afterSeq }
            if let throughStr = queryMap["through_seq"], let throughSeq = Int(throughStr) {
                filtered = filtered.filter { (($0["seq"] as? Int) ?? 0) <= throughSeq }
            }
            if filtered.count > limit {
                filtered = Array(filtered.prefix(limit))
            }
        } else if let beforeStr = queryMap["before_seq"], let beforeSeq = Int(beforeStr) {
            filtered = filtered.filter { (($0["seq"] as? Int) ?? 0) < beforeSeq }
            if filtered.count > limit {
                filtered = Array(filtered.suffix(limit))
            }
        } else {
            // Newest window
            if filtered.count > limit {
                filtered = Array(filtered.suffix(limit))
            }
        }

        let totalMaxSeq = allMsgs.compactMap { $0["seq"] as? Int }.max() ?? 0
        let pageMinSeq = filtered.compactMap { $0["seq"] as? Int }.min()
        let pageMaxSeq = filtered.compactMap { $0["seq"] as? Int }.max()

        let hasOlder: Bool
        if let pageMin = pageMinSeq {
            hasOlder = allMsgs.contains { (($0["seq"] as? Int) ?? 0) < pageMin }
        } else {
            hasOlder = false
        }

        let hasNewer: Bool
        if let pageMax = pageMaxSeq {
            hasNewer = allMsgs.contains { (($0["seq"] as? Int) ?? 0) > pageMax }
        } else {
            hasNewer = false
        }

        var response: [String: Any] = [
            "messages": filtered,
            "max_seq": totalMaxSeq,
            "has_older": hasOlder,
            "has_newer": hasNewer,
        ]
        if let pageMinSeq {
            response["page_min_seq"] = pageMinSeq
        }
        if let pageMaxSeq {
            response["page_max_seq"] = pageMaxSeq
        }

        return .json(status: 200, response)
    }

    public func submitTurn(sessionID: String, text: String) {
        lock.withLock {
            var list = messagesBySessionID[sessionID] ?? defaultMessages(for: sessionID)
            let nextSeq = (list.map { ($0["seq"] as? Int) ?? 0 }.max() ?? 0) + 1

            let userMsg: [String: Any] = [
                "event_id": "turn-user-\(nextSeq)",
                "seq": nextSeq,
                "type": "user_input",
                "role": "user",
                "text": text,
                "payload": [:] as [String: Any],
            ]
            list.append(userMsg)
            broadcastToStreamLocked(sessionID: sessionID, message: userMsg)

            let replySeq = nextSeq + 1
            let replyMsg: [String: Any] = [
                "event_id": "turn-reply-\(replySeq)",
                "seq": replySeq,
                "type": "assistant_output",
                "role": "assistant",
                "text": "I've added the benchmark tests in `benchmarks/json_bench.rs`. The throughput is ~120 MB/s. Everything looks ready to commit!\n\n_Demo mode: replies are simulated._",
                "payload": [:] as [String: Any],
            ]
            list.append(replyMsg)
            broadcastToStreamLocked(sessionID: sessionID, message: replyMsg)

            messagesBySessionID[sessionID] = list

            if let idx = sessions.firstIndex(where: { ($0["session_id"] as? String) == sessionID }) {
                sessions[idx]["awaiting"] = NSNull()
                sessions[idx]["last_activity"] = ISO8601DateFormatter().string(from: Date())
            }
        }
    }

    private func handleTurnSubmission(sessionID: String, request: URLRequest) -> DemoHubResponse {
        var userText = "Sample follow-up message"
        if let body = request.bodyData(),
           let payload = try? JSONSerialization.jsonObject(with: body) as? [String: Any],
           let text = payload["text"] as? String {
            userText = text
        }

        submitTurn(sessionID: sessionID, text: userText)
        return .json(status: 202, ["turn_id": "demo-turn-\(UUID().uuidString)"])
    }

    public func decidePermission(sessionID: String, requestID: String, decision: String) {
        lock.withLock {
            var list = messagesBySessionID[sessionID] ?? defaultMessages(for: sessionID)
            let nextSeq = (list.map { ($0["seq"] as? Int) ?? 0 }.max() ?? 0) + 1

            let respMsg: [String: Any] = [
                "event_id": "appr-resp-\(nextSeq)",
                "seq": nextSeq,
                "type": "approval_response",
                "role": "user",
                "text": decision,
                "payload": [
                    "request_id": requestID,
                    "decision": decision,
                ] as [String: Any],
            ]
            list.append(respMsg)
            broadcastToStreamLocked(sessionID: sessionID, message: respMsg)

            let resultSeq = nextSeq + 1
            if decision == "allow" {
                let toolResMsg: [String: Any] = [
                    "event_id": "tool-res-\(resultSeq)",
                    "seq": resultSeq,
                    "type": "tool_result",
                    "role": "tool",
                    "text": "CREATE TABLE user_sessions (id UUID PRIMARY KEY, user_id UUID NOT NULL, expires_at TIMESTAMP WITH TIME ZONE);\nCREATE INDEX idx_user_sessions_user_id ON user_sessions(user_id);\nCOMMIT;\n1 migration applied.",
                    "payload": [:] as [String: Any],
                ]
                list.append(toolResMsg)
                broadcastToStreamLocked(sessionID: sessionID, message: toolResMsg)

                let assistMsg: [String: Any] = [
                    "event_id": "assist-\(resultSeq + 1)",
                    "seq": resultSeq + 1,
                    "type": "assistant_output",
                    "role": "assistant",
                    "text": "Migration 004 applied successfully. 3 tables and index created.",
                    "payload": [:] as [String: Any],
                ]
                list.append(assistMsg)
                broadcastToStreamLocked(sessionID: sessionID, message: assistMsg)
            } else {
                let toolResMsg: [String: Any] = [
                    "event_id": "tool-res-\(resultSeq)",
                    "seq": resultSeq,
                    "type": "tool_result",
                    "role": "tool",
                    "text": "Permission denied by operator.",
                    "payload": [:] as [String: Any],
                ]
                list.append(toolResMsg)
                broadcastToStreamLocked(sessionID: sessionID, message: toolResMsg)

                let assistMsg: [String: Any] = [
                    "event_id": "assist-\(resultSeq + 1)",
                    "seq": resultSeq + 1,
                    "type": "assistant_output",
                    "role": "assistant",
                    "text": "Database migration skipped. Proceeding without database schema update.",
                    "payload": [:] as [String: Any],
                ]
                list.append(assistMsg)
                broadcastToStreamLocked(sessionID: sessionID, message: assistMsg)
            }

            messagesBySessionID[sessionID] = list

            if let idx = sessions.firstIndex(where: { ($0["session_id"] as? String) == sessionID }) {
                sessions[idx]["awaiting"] = NSNull()
                sessions[idx]["last_activity"] = ISO8601DateFormatter().string(from: Date())
            }
        }
    }

    private func handlePermissionDecision(sessionID: String, request: URLRequest) -> DemoHubResponse {
        var decision = "allow"
        var requestID = DemoHubData.approvalRequestID

        if let body = request.bodyData(),
           let payload = try? JSONSerialization.jsonObject(with: body) as? [String: Any] {
            if let d = payload["decision"] as? String { decision = d }
            if let r = payload["request_id"] as? String { requestID = r }
        }

        decidePermission(sessionID: sessionID, requestID: requestID, decision: decision)
        return .json(status: 200, ["status": "ok"])
    }

    public func interruptSession(sessionID: String) {
        lock.withLock {
            var list = messagesBySessionID[sessionID] ?? defaultMessages(for: sessionID)
            let nextSeq = (list.map { ($0["seq"] as? Int) ?? 0 }.max() ?? 0) + 1

            let intrMsg: [String: Any] = [
                "event_id": "intr-\(nextSeq)",
                "seq": nextSeq,
                "type": "assistant_output",
                "role": "assistant",
                "text": "Process interrupted by user (SIGINT delivered).",
                "payload": [:] as [String: Any],
            ]
            list.append(intrMsg)
            broadcastToStreamLocked(sessionID: sessionID, message: intrMsg)
            messagesBySessionID[sessionID] = list
        }
    }

    private func handleInterrupt(sessionID: String) -> DemoHubResponse {
        interruptSession(sessionID: sessionID)
        return .json(status: 200, ["status": "ok"])
    }

    public func terminateSession(sessionID: String) {
        lock.withLock {
            if let idx = sessions.firstIndex(where: { ($0["session_id"] as? String) == sessionID }) {
                sessions[idx]["status"] = "terminated"
                sessions[idx]["awaiting"] = NSNull()
                sessions[idx]["last_activity"] = ISO8601DateFormatter().string(from: Date())
            }
        }
    }

    private func handleTerminate(sessionID: String) -> DemoHubResponse {
        terminateSession(sessionID: sessionID)
        return .json(status: 200, ["status": "ok", "state": "terminated"])
    }

    private func defaultMessages(for sessionID: String) -> [[String: Any]] {
        [
            [
                "event_id": "def-1",
                "seq": 1,
                "type": "user_input",
                "role": "user",
                "text": "Task inspection for \(sessionID)",
                "payload": [:] as [String: Any],
            ],
            [
                "event_id": "def-2",
                "seq": 2,
                "type": "assistant_output",
                "role": "assistant",
                "text": "Ready and connected to demo session.",
                "payload": [:] as [String: Any],
            ],
        ]
    }
}

private extension URLRequest {
    func bodyData() -> Data? {
        if let httpBody { return httpBody }
        guard let stream = httpBodyStream else { return nil }
        stream.open()
        defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 4_096)
        while stream.hasBytesAvailable {
            let count = stream.read(&buffer, maxLength: buffer.count)
            guard count > 0 else { break }
            data.append(buffer, count: count)
        }
        return data
    }
}
