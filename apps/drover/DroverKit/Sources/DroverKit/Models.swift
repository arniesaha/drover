import Foundation

// MARK: - Date parsing

enum WireDate {
    // ISO8601DateFormatter is not marked Sendable by Foundation, but its
    // documented behavior is safe for concurrent reads once configured and
    // never mutated afterward, which is how these are used.
    nonisolated(unsafe) static let withFractionalSeconds: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()

    nonisolated(unsafe) static let withoutFractionalSeconds: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter
    }()

    /// The hub serializes datetimes with Python's `str(datetime)`:
    /// "2026-07-30 10:12:03.123456+00:00" — space separator, fraction and
    /// offset both optional. Naive timestamps are UTC (same assumption as
    /// the web UI's normalizer in static/harness.html).
    /// DateFormatter parsing has been thread-safe since iOS 7; the plain
    /// static needs no `nonisolated(unsafe)`, unlike the ISO formatters above.
    private static let serverFormatters: [DateFormatter] = [
        "yyyy-MM-dd HH:mm:ss.SSSSSSxxxxx",
        "yyyy-MM-dd HH:mm:ss.SSSSSS",
        "yyyy-MM-dd HH:mm:ssxxxxx",
        "yyyy-MM-dd HH:mm:ss",
    ].map { pattern in
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(identifier: "UTC")
        formatter.dateFormat = pattern
        return formatter
    }

    private static func parseServerFormat(_ value: String) -> Date? {
        for formatter in serverFormatters {
            if let date = formatter.date(from: value) { return date }
        }
        return nil
    }

    static func parse(_ string: String?) -> Date? {
        guard let string else { return nil }
        if let date = withFractionalSeconds.date(from: string) {
            return date
        }
        if let date = withoutFractionalSeconds.date(from: string) {
            return date
        }
        return parseServerFormat(string)
    }
}

// MARK: - Lenient array decoding

/// Wraps a `Decodable` element so a single bad element doesn't fail the whole array.
/// Decode `[LenientElement<T>].compactMap(\.value)` to skip bad elements.
private struct LenientElement<T: Decodable>: Decodable {
    let value: T?
    let errorDetail: String?
    let seq: Int?

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        do {
            value = try container.decode(T.self)
            errorDetail = nil
            seq = nil
        } catch {
            value = nil
            errorDetail = String(describing: error)
            seq = (try? container.decode(SequenceProbe.self))?.seq
        }
    }
}

private struct SequenceProbe: Decodable {
    let seq: Int?
}

private func lenientDecode<T: Decodable>(_ type: T.Type, from array: [LenientElement<T>]) -> [T] {
    array.compactMap(\.value)
}

// MARK: - AttentionState

public enum AttentionState: String, Sendable {
    case needsApproval, needsInput, working, done, errored
}

// MARK: - Harness auth

public enum HarnessAuthState: String, Sendable, Equatable, Decodable {
    case authenticated
    case unauthenticated
    case unknown
    case unavailable
    case starting
    case waitingForUser = "waiting_for_user"
    case failed
    case cancelled
    case expired

    public init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = HarnessAuthState(rawValue: raw) ?? .unknown
    }
}

public struct HarnessAuthStatus: Sendable, Equatable, Decodable {
    public var hostID: String
    public var harness: String
    public var state: HarnessAuthState
    public var label: String?
    public var detail: String?

    private enum CodingKeys: String, CodingKey {
        case hostID = "host_id"
        case harness
        case state
        case label
        case detail
    }
}

public struct HarnessAuthFlow: Sendable, Equatable, Decodable {
    public var hostID: String
    public var harness: String
    public var flowID: String
    public var state: HarnessAuthState
    public var loginURL: URL?
    public var deviceCode: String?
    public var userCode: String?
    public var message: String?
    public var expiresAt: Date?
    public var lastError: String?

    private enum CodingKeys: String, CodingKey {
        case hostID = "host_id"
        case harness
        case flowID = "flow_id"
        case state
        case loginURL = "login_url"
        case deviceCode = "device_code"
        case userCode = "user_code"
        case message
        case expiresAt = "expires_at"
        case lastError = "last_error"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        hostID = (try? container.decode(String.self, forKey: .hostID)) ?? ""
        harness = try container.decode(String.self, forKey: .harness)
        flowID = try container.decode(String.self, forKey: .flowID)
        state = try container.decode(HarnessAuthState.self, forKey: .state)
        if let raw = try? container.decode(String.self, forKey: .loginURL) {
            let parsed = URL(string: raw)
            let scheme = parsed?.scheme?.lowercased()
            if (scheme == "http" || scheme == "https"), parsed?.host?.isEmpty == false {
                loginURL = parsed
            }
        }
        deviceCode = try? container.decode(String.self, forKey: .deviceCode)
        userCode = try? container.decode(String.self, forKey: .userCode)
        message = try? container.decode(String.self, forKey: .message)
        let rawExpires = try? container.decode(String.self, forKey: .expiresAt)
        expiresAt = WireDate.parse(rawExpires)
        lastError = try? container.decode(String.self, forKey: .lastError)
    }

    public var isTerminal: Bool {
        state == .authenticated || state == .failed || state == .cancelled || state == .expired
    }
}

// MARK: - JSONValue

public enum JSONValue: Sendable, Equatable, Decodable {
    case string(String), number(Double), bool(Bool), null
    case array([JSONValue]), object([String: JSONValue])

    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if let boolValue = try? container.decode(Bool.self) {
            self = .bool(boolValue)
        } else if let numberValue = try? container.decode(Double.self) {
            self = .number(numberValue)
        } else if let stringValue = try? container.decode(String.self) {
            self = .string(stringValue)
        } else if let arrayValue = try? container.decode([JSONValue].self) {
            self = .array(arrayValue)
        } else if let objectValue = try? container.decode([String: JSONValue].self) {
            self = .object(objectValue)
        } else if container.decodeNil() {
            self = .null
        } else {
            self = .null
        }
    }

    public var stringValue: String? {
        if case .string(let value) = self { return value }
        return nil
    }

    public var boolValue: Bool? {
        if case .bool(let value) = self { return value }
        return nil
    }

    public var numberValue: Double? {
        if case .number(let value) = self { return value }
        return nil
    }

    public var objectValue: [String: JSONValue]? {
        if case .object(let value) = self { return value }
        return nil
    }

    /// Compact single-line, human-readable rendering used by the chat
    /// screen's tool cards and approval banner (e.g. a tool call's `input`
    /// object). Not meant to round-trip — purely presentational.
    public var displayString: String {
        switch self {
        case .string(let value): return value
        case .bool(let value): return value ? "true" : "false"
        case .null: return "null"
        case .number(let value):
            return value.truncatingRemainder(dividingBy: 1) == 0 ? String(Int(value)) : String(value)
        case .array(let values):
            return "[" + values.map(\.displayString).joined(separator: ", ") + "]"
        case .object(let values):
            return "{" + values.keys.sorted().map { "\($0): \(values[$0]!.displayString)" }.joined(separator: ", ") + "}"
        }
    }
}

// MARK: - HostSummary

public struct HostSummary: Sendable, Identifiable, Decodable, Equatable, Hashable {
    public var id: String        // host_id
    public var displayName: String
    public var status: String    // "online"/"stale"/"offline"
    public var connectionKind: String
    public var lastSeenAt: Date?
    public var harnesses: [String]  // enabled preset names from capabilities

    public init(
        id: String,
        displayName: String,
        status: String,
        connectionKind: String = "direct",
        lastSeenAt: Date? = nil,
        harnesses: [String] = []
    ) {
        self.id = id
        self.displayName = displayName
        self.status = status
        self.connectionKind = connectionKind
        self.lastSeenAt = lastSeenAt
        self.harnesses = harnesses
    }

    private enum CodingKeys: String, CodingKey {
        case id = "host_id"
        case status
        case connectionKind = "connection_kind"
        case lastSeenAt = "last_seen_at"
        case capabilities
    }

    private enum CapabilitiesKeys: String, CodingKey {
        case displayName = "display_name"
        case harnesses
    }

    private struct HarnessEntry: Decodable {
        let name: String
        let enabled: Bool
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        status = (try? container.decode(String.self, forKey: .status)) ?? ""
        connectionKind = (try? container.decode(String.self, forKey: .connectionKind)) ?? "direct"
        if let raw = try? container.decode(String.self, forKey: .lastSeenAt) {
            lastSeenAt = WireDate.parse(raw)
        } else {
            lastSeenAt = nil
        }
        if let caps = try? container.nestedContainer(keyedBy: CapabilitiesKeys.self, forKey: .capabilities) {
            displayName = (try? caps.decode(String.self, forKey: .displayName)) ?? ""
            let entriesWrapped = (try? caps.decode([LenientElement<HarnessEntry>].self, forKey: .harnesses)) ?? []
            let entries = lenientDecode(HarnessEntry.self, from: entriesWrapped)
            harnesses = entries.filter(\.enabled).map(\.name)
        } else {
            displayName = ""
            harnesses = []
        }
    }
}

/// Three-way host presence. Relay hosts are socket-truth online/offline
/// (never stale); direct hosts are heartbeat-based online/stale (never
/// offline). Unknown/empty statuses render as offline.
public enum HostPresence: String, Sendable {
    case online, stale, offline
}

extension HostSummary {
    public var presence: HostPresence {
        switch status {
        case "online": return .online
        case "stale": return .stale
        default: return .offline
        }
    }

    public var isRelay: Bool { connectionKind == "relay" }

    public var title: String { displayName.isEmpty ? id : displayName }
}

// MARK: - SessionSummary

public struct SessionSummary: Sendable, Identifiable, Decodable, Equatable {
    public var id: String        // session_id
    public var hostID: String
    public var harness: String
    /// "pty" | "structured" | nil. `nil` means the wire omitted the key or
    /// sent JSON null — legacy sessions predating the field. Do NOT collapse
    /// that to a default string here; `isStructured` interprets the
    /// null-vs-absent case per-harness (see below).
    public var mode: String?
    public var status: String
    public var awaiting: String?
    public var cwd: String?
    public var lastActivity: Date?
    public var preview: String?
    public var startedAt: Date?
    public var updatedAt: Date?
    public var endedAt: Date?
    public var model: String?
    public var thinkingEffort: String?

    public init(
        id: String,
        hostID: String,
        harness: String,
        mode: String?,
        status: String,
        awaiting: String?,
        cwd: String?,
        lastActivity: Date?,
        preview: String? = nil,
        startedAt: Date? = nil,
        updatedAt: Date? = nil,
        endedAt: Date? = nil,
        model: String? = nil,
        thinkingEffort: String? = nil
    ) {
        self.id = id
        self.hostID = hostID
        self.harness = harness
        self.mode = mode
        self.status = status
        self.awaiting = awaiting
        self.cwd = cwd
        self.lastActivity = lastActivity
        self.preview = preview
        self.startedAt = startedAt
        self.updatedAt = updatedAt
        self.endedAt = endedAt
        self.model = model
        self.thinkingEffort = thinkingEffort
    }

    private enum CodingKeys: String, CodingKey {
        case id = "session_id"
        case hostID = "host_id"
        case harness
        case mode
        case status
        case awaiting
        case cwd
        case lastActivity = "last_activity"
        case preview
        case startedAt = "started_at"
        case updatedAt = "updated_at"
        case endedAt = "ended_at"
        case model
        case thinkingEffort = "thinking_effort"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        hostID = (try? container.decode(String.self, forKey: .hostID)) ?? ""
        harness = (try? container.decode(String.self, forKey: .harness)) ?? ""
        mode = try? container.decode(String.self, forKey: .mode)
        status = (try? container.decode(String.self, forKey: .status)) ?? ""
        awaiting = try? container.decode(String.self, forKey: .awaiting)
        cwd = try? container.decode(String.self, forKey: .cwd)
        let rawDate = try? container.decode(String.self, forKey: .lastActivity)
        lastActivity = WireDate.parse(rawDate)
        preview = try? container.decode(String.self, forKey: .preview)
        let rawStartedAt = try? container.decode(String.self, forKey: .startedAt)
        startedAt = WireDate.parse(rawStartedAt)
        let rawUpdatedAt = try? container.decode(String.self, forKey: .updatedAt)
        updatedAt = WireDate.parse(rawUpdatedAt)
        let rawEndedAt = try? container.decode(String.self, forKey: .endedAt)
        endedAt = WireDate.parse(rawEndedAt)
        model = try? container.decode(String.self, forKey: .model)
        thinkingEffort = try? container.decode(String.self, forKey: .thinkingEffort)
    }

    public var attention: AttentionState {
        switch status {
        case "completed", "terminated":
            return .done
        case "errored":
            return .errored
        default:
            if awaiting == "approval" {
                return .needsApproval
            } else if awaiting == "input" {
                return .needsInput
            } else {
                return .working
            }
        }
    }

    public var activityDate: Date? {
        lastActivity ?? updatedAt ?? startedAt
    }

    /// Legacy sessions predating the `mode` field send neither the key nor a
    /// value — a `nil` here does not mean PTY, it means "ask the harness".
    /// Every harness except "shell" only ever runs in structured mode, so a
    /// null-mode claude-code/codex/gemini session is structured too. Mirrors
    /// `LaunchModel.isStructured`'s harness-based fallback for the
    /// pre-creation case.
    public var isStructured: Bool {
        mode == "structured" || (mode == nil && harness != "shell")
    }
}

// MARK: - CwdSuggestion

/// One working-directory suggestion for the launch sheet. The server sends
/// objects ({path, source, host_id}), not bare strings — `source` is
/// "recent session" or "favorite", and `host_id` (absent for favorites)
/// scopes a suggestion to the host it was seen on.
public struct CwdSuggestion: Sendable, Equatable, Decodable {
    public var path: String
    public var source: String
    public var hostID: String?

    public init(path: String, source: String = "", hostID: String? = nil) {
        self.path = path
        self.source = source
        self.hostID = hostID
    }

    private enum CodingKeys: String, CodingKey {
        case path
        case source
        case hostID = "host_id"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        path = try container.decode(String.self, forKey: .path)
        source = (try? container.decode(String.self, forKey: .source)) ?? ""
        hostID = try? container.decode(String.self, forKey: .hostID)
    }
}

// MARK: - HarnessSnapshot

public struct HarnessSnapshot: Sendable, Decodable {
    public var hosts: [HostSummary]
    public var sessions: [SessionSummary]
    public var cwdSuggestions: [CwdSuggestion]

    private enum CodingKeys: String, CodingKey {
        case hosts
        case sessions
        case cwdSuggestions = "cwd_suggestions"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let hostsWrapped = (try? container.decode([LenientElement<HostSummary>].self, forKey: .hosts)) ?? []
        hosts = lenientDecode(HostSummary.self, from: hostsWrapped)
        let sessionsWrapped = (try? container.decode([LenientElement<SessionSummary>].self, forKey: .sessions)) ?? []
        sessions = lenientDecode(SessionSummary.self, from: sessionsWrapped)
        let suggestionsWrapped = (try? container.decode([LenientElement<CwdSuggestion>].self, forKey: .cwdSuggestions)) ?? []
        cwdSuggestions = lenientDecode(CwdSuggestion.self, from: suggestionsWrapped)
    }

    public static func decode(from data: Data) throws -> HarnessSnapshot {
        try JSONDecoder().decode(HarnessSnapshot.self, from: data)
    }
}

// MARK: - MessageType

public enum MessageType: String, Sendable, Equatable {
    case assistantOutput = "assistant_output"
    case userInput = "user_input"
    case toolAction = "tool_action"
    case toolResult = "tool_result"
    case approvalPrompt = "approval_prompt"
    case approvalResponse = "approval_response"
    case status, error, raw
    case unknown  // any unrecognized wire string decodes to this

    public init(wire: String) {
        self = MessageType(rawValue: wire) ?? .unknown
    }
}

// MARK: - HarnessMessage

public struct HarnessMessage: Sendable, Identifiable, Decodable, Equatable {
    public var id: String        // event_id
    public var seq: Int
    public var type: MessageType
    public var role: String
    public var text: String
    public var turnID: String?
    public var timestamp: Date?
    public var payload: [String: JSONValue]  // tolerant free-form
    /// `text` with inline markdown parsed, computed exactly once here so the
    /// transcript never re-parses on render passes — `Text(LocalizedStringKey)`
    /// parses markdown on every pass, which saturates the main thread during
    /// long streams (a contributor to the LazyVStack blanking).
    public var displayText: AttributedString
    /// `text` split at ``` fences into renderable segments, computed exactly
    /// once here for the same reason as `displayText`: segmentation in a view
    /// body would re-run on every render pass during streams.
    public var displayBlocks: [DisplayBlock]

    private static func parseDisplayText(_ text: String) -> AttributedString {
        DisplayBlock.parseInlineMarkdown(text)
    }

    private enum CodingKeys: String, CodingKey {
        case id = "event_id"
        case seq
        case type
        case role
        case text
        case turnID = "turn_id"
        case timestamp = "ts"
        case payload
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        seq = (try? container.decode(Int.self, forKey: .seq)) ?? 0
        let rawType = (try? container.decode(String.self, forKey: .type)) ?? ""
        type = MessageType(wire: rawType)
        role = (try? container.decode(String.self, forKey: .role)) ?? ""
        text = (try? container.decode(String.self, forKey: .text)) ?? ""
        turnID = try? container.decode(String.self, forKey: .turnID)
        let rawTimestamp = try? container.decode(String.self, forKey: .timestamp)
        timestamp = WireDate.parse(rawTimestamp)
        payload = (try? container.decode([String: JSONValue].self, forKey: .payload)) ?? [:]
        displayText = Self.parseDisplayText(text)
        displayBlocks = DisplayBlock.segment(text)
    }

    /// Test-only direct construction, bypassing JSON decoding entirely. Not
    /// `public` — reachable only via `@testable import DroverKit` from this
    /// package's own test targets (see `HarnessMessage.fixture` in test
    /// support).
    init(
        id: String = UUID().uuidString,
        seq: Int,
        type: MessageType,
        role: String = "assistant",
        text: String = "",
        turnID: String? = nil,
        timestamp: Date? = nil,
        payload: [String: JSONValue] = [:]
    ) {
        self.id = id
        self.seq = seq
        self.type = type
        self.role = role
        self.text = text
        self.turnID = turnID
        self.timestamp = timestamp
        self.payload = payload
        self.displayText = Self.parseDisplayText(text)
        self.displayBlocks = DisplayBlock.segment(text)
    }
}

// MARK: - MessageBatch

public struct MessageBatch: Sendable {
    public var messages: [HarnessMessage]
    public var maxSeq: Int

    private enum CodingKeys: String, CodingKey {
        case messages
        case maxSeq = "max_seq"
    }

    public static func decode(from data: Data) throws -> MessageBatch {
        let container = try JSONDecoder().decode(RawMessageBatch.self, from: data)
        return MessageBatch(messages: container.messages, maxSeq: container.maxSeq)
    }

    private struct RawMessageBatch: Decodable {
        let messages: [HarnessMessage]
        let maxSeq: Int

        private enum CodingKeys: String, CodingKey {
            case messages
            case maxSeq = "max_seq"
        }

        init(from decoder: Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            let messagesWrapped = (try? container.decode([LenientElement<HarnessMessage>].self, forKey: .messages)) ?? []
            messages = lenientDecode(HarnessMessage.self, from: messagesWrapped)
            maxSeq = (try? container.decode(Int.self, forKey: .maxSeq)) ?? 0
        }
    }
}

// MARK: - MessagePage

public struct MessageDecodeIssue: Sendable, Equatable {
    public let index: Int
    public let seq: Int?
    public let detail: String

    public init(index: Int, seq: Int?, detail: String) {
        self.index = index
        self.seq = seq
        self.detail = detail
    }
}

public struct MessagePage: Sendable {
    public let messages: [HarnessMessage]
    public let pageMinSeq: Int?
    public let pageMaxSeq: Int?
    public let maxSeq: Int
    public let hasOlder: Bool
    public let hasNewer: Bool
    public let decodeIssues: [MessageDecodeIssue]

    public static func decode(from data: Data) throws -> MessagePage {
        try JSONDecoder().decode(MessagePage.self, from: data)
    }
}

extension MessagePage: Decodable {
    private enum CodingKeys: String, CodingKey {
        case messages
        case pageMinSeq = "page_min_seq"
        case pageMaxSeq = "page_max_seq"
        case maxSeq = "max_seq"
        case hasOlder = "has_older"
        case hasNewer = "has_newer"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let wrapped = (try? container.decode(
            [LenientElement<HarnessMessage>].self, forKey: .messages
        )) ?? []
        messages = lenientDecode(HarnessMessage.self, from: wrapped)
        decodeIssues = wrapped.enumerated().compactMap { index, element in
            guard element.value == nil else { return nil }
            return MessageDecodeIssue(
                index: index,
                seq: element.seq,
                detail: element.errorDetail ?? "message could not be decoded"
            )
        }
        pageMinSeq = try? container.decode(Int.self, forKey: .pageMinSeq)
        pageMaxSeq = try? container.decode(Int.self, forKey: .pageMaxSeq)
        maxSeq = (try? container.decode(Int.self, forKey: .maxSeq)) ?? 0
        hasOlder = (try? container.decode(Bool.self, forKey: .hasOlder)) ?? false
        hasNewer = (try? container.decode(Bool.self, forKey: .hasNewer)) ?? false
    }
}
