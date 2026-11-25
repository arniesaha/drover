import Foundation

/// Result visibility is independent of execution status and notification delivery.
/// Storage is hub-scoped. Polling never acknowledges a result; only a rendered
/// assistant response or an explicit dismiss can do that.
public struct ResultReadReceipts {
    private let defaults: UserDefaults
    private let prefix: String

    public init(hub: String, defaults: UserDefaults = .standard) {
        self.defaults = defaults
        prefix = "drover.result-read.v1." + hub + "."
    }

    public static func generation(_ session: SessionSummary) -> String? {
        guard session.hostRetiredAt == nil,
              session.attention == .done || session.attention == .errored else { return nil }
        if let id = session.attentionID { return "attention:" + id }
        guard let date = session.activityDate else { return nil }
        return "activity:\(date.timeIntervalSince1970)"
    }

    /// First-install migration: preserve one latest result per host for review,
    /// rather than flooding Home with every old finished session. All subsequent
    /// results persist without a timeout. Missing/filtered snapshots never prune.
    public func bootstrap(_ sessions: [SessionSummary]) {
        guard !defaults.bool(forKey: prefix + "bootstrapped") else { return }
        let finished = sessions.filter { Self.generation($0) != nil }
        let newest = Dictionary(grouping: finished, by: \.hostID).values.compactMap {
            $0.max { ($0.activityDate ?? .distantPast) < ($1.activityDate ?? .distantPast) }?.id
        }
        var dismissed = defaults.dictionary(forKey: prefix + "dismissed") as? [String: String] ?? [:]
        for session in finished where !newest.contains(session.id) {
            dismissed[session.id] = Self.generation(session)
        }
        defaults.set(dismissed, forKey: prefix + "dismissed")
        defaults.set(true, forKey: prefix + "bootstrapped")
    }

    public func unread(in sessions: [SessionSummary]) -> [SessionSummary] {
        let dismissed = defaults.dictionary(forKey: prefix + "dismissed") as? [String: String] ?? [:]
        let read = defaults.dictionary(forKey: prefix + "viewed") as? [String: [String]] ?? [:]
        return sessions.filter { session in
            guard let generation = Self.generation(session),
                  dismissed[session.id] != generation else { return false }
            if read[session.id]?.contains(generation) == true { return false }
            return true
        }.sorted { ($0.activityDate ?? .distantPast) > ($1.activityDate ?? .distantPast) }
    }

    /// Call only for an actual visible, non-thinking assistant response, not for
    /// a loaded transcript or a session destination appearing/disappearing.
    public func viewed(sessionID: String, response: HarnessMessage, messages: [HarnessMessage]) {
        guard response.type == .assistantOutput, !response.isThinking,
              !response.text.isEmpty,
              let latestResponse = messages.last(where: {
                  $0.type == .assistantOutput && !$0.isThinking && !$0.text.isEmpty
              }), latestResponse.seq == response.seq,
              let completion = messages.last(where: {
                  $0.type == .status && (
                      $0.payload["turn_complete"]?.boolValue == true
                      || $0.payload["exited"] != nil
                      || $0.payload["awaiting"]?.stringValue == "input"
                  )
              }), completion.seq >= response.seq,
              !messages.contains(where: {
                  $0.seq > completion.seq && ($0.type == .userInput || $0.type == .assistantOutput)
              }) else { return }
        var generations = ["attention:" + completion.id]
        if let timestamp = completion.timestamp {
            generations.append("activity:\(timestamp.timeIntervalSince1970)")
        }
        var read = defaults.dictionary(forKey: prefix + "viewed") as? [String: [String]] ?? [:]
        read[sessionID] = generations
        defaults.set(read, forKey: prefix + "viewed")
    }

    public func dismiss(_ session: SessionSummary) {
        guard let generation = Self.generation(session) else { return }
        var dismissed = defaults.dictionary(forKey: prefix + "dismissed") as? [String: String] ?? [:]
        dismissed[session.id] = generation
        defaults.set(dismissed, forKey: prefix + "dismissed")
    }
}
