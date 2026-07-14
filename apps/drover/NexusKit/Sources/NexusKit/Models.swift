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

    static func parse(_ string: String?) -> Date? {
        guard let string else { return nil }
        if let date = withFractionalSeconds.date(from: string) {
            return date
        }
        return withoutFractionalSeconds.date(from: string)
    }
}

// MARK: - Lenient array decoding

/// Wraps a `Decodable` element so a single bad element doesn't fail the whole array.
/// Decode `[LenientElement<T>].compactMap(\.value)` to skip bad elements.
private struct LenientElement<T: Decodable>: Decodable {
    let value: T?

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        value = try? container.decode(T.self)
    }
}

private func lenientDecode<T: Decodable>(_ type: T.Type, from array: [LenientElement<T>]) -> [T] {
    array.compactMap(\.value)
}

// MARK: - AttentionState

public enum AttentionState: String, Sendable {
    case needsApproval, needsInput, working, done, errored
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

public struct HostSummary: Sendable, Identifiable, Decodable {
    public var id: String        // host_id
    public var displayName: String
    public var status: String    // "online"/"offline"
    public var harnesses: [String]  // enabled preset names from capabilities

    private enum CodingKeys: String, CodingKey {
        case id = "host_id"
        case status
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

        if let capabilities = try? container.nestedContainer(keyedBy: CapabilitiesKeys.self, forKey: .capabilities) {
            displayName = (try? capabilities.decode(String.self, forKey: .displayName)) ?? ""
            let entriesWrapped = (try? capabilities.decode([LenientElement<HarnessEntry>].self, forKey: .harnesses)) ?? []
            let entries = lenientDecode(HarnessEntry.self, from: entriesWrapped)
            harnesses = entries.filter(\.enabled).map(\.name)
        } else {
            displayName = ""
            harnesses = []
        }
    }
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

    public init(
        id: String,
        hostID: String,
        harness: String,
        mode: String?,
        status: String,
        awaiting: String?,
        cwd: String?,
        lastActivity: Date?
    ) {
        self.id = id
        self.hostID = hostID
        self.harness = harness
        self.mode = mode
        self.status = status
        self.awaiting = awaiting
        self.cwd = cwd
        self.lastActivity = lastActivity
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

    private static func parseDisplayText(_ text: String) -> AttributedString {
        let options = AttributedString.MarkdownParsingOptions(
            interpretedSyntax: .inlineOnlyPreservingWhitespace)
        return (try? AttributedString(markdown: text, options: options))
            ?? AttributedString(text)
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
    }

    /// Test-only direct construction, bypassing JSON decoding entirely. Not
    /// `public` — reachable only via `@testable import NexusKit` from this
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
