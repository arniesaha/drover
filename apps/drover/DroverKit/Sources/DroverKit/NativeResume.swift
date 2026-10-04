import Foundation

/// A provider-native session a host can resume (#422). Hosts list these only
/// for harnesses whose adapter advertises `native_resume`, and only adapters
/// with a discovery extension list any. The app never infers either from a
/// harness name.
public struct NativeResumeCandidate: Sendable, Equatable, Hashable, Identifiable, Decodable {
    /// The harness whose adapter discovered it.
    public var harness: String
    /// What the hub sends back as `native_resume`.
    public var nativeSessionID: String
    public var label: String
    public var cwd: String?

    public var id: String { "\(harness)\u{1f}\(nativeSessionID)" }

    public init(harness: String, nativeSessionID: String, label: String, cwd: String? = nil) {
        self.harness = harness
        self.nativeSessionID = nativeSessionID
        self.label = label
        self.cwd = cwd
    }

    private enum CodingKeys: String, CodingKey {
        case harness, label, cwd
        case sessionID = "session_id"
        case nativeResume = "native_resume"
    }

    private enum NativeKeys: String, CodingKey {
        case sessionID = "session_id"
        case label
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        harness = try container.decode(String.self, forKey: .harness)
        let native = try container.nestedContainer(keyedBy: NativeKeys.self, forKey: .nativeResume)
        nativeSessionID = try native.decode(String.self, forKey: .sessionID)
        guard !nativeSessionID.isEmpty else {
            throw DecodingError.dataCorruptedError(
                forKey: .sessionID, in: native, debugDescription: "empty native session ID")
        }
        label = try native.decodeIfPresent(String.self, forKey: .label)
            ?? container.decodeIfPresent(String.self, forKey: .label)
            ?? nativeSessionID
        cwd = try container.decodeIfPresent(String.self, forKey: .cwd)
    }

    /// The `native_resume` object a Continue request carries.
    var wirePayload: [String: String] {
        ["session_id": nativeSessionID, "label": label]
    }
}

/// `GET /harness/hosts/{id}/native-sessions`. Malformed rows are dropped
/// rather than failing the whole list.
struct NativeResumeCandidateList: Decodable {
    var sessions: [NativeResumeCandidate]

    private enum CodingKeys: String, CodingKey { case sessions }

    private struct Lossy: Decodable {
        var value: NativeResumeCandidate?
        init(from decoder: Decoder) throws {
            value = try? NativeResumeCandidate(from: decoder)
        }
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        sessions = (try container.decodeIfPresent([Lossy].self, forKey: .sessions) ?? [])
            .compactMap(\.value)
    }
}
