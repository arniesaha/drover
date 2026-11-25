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

    /// Lifecycle termination is not response completion. Preserve the real
    /// completed-turn identity even when the process subsequently stops.
    public static func completedResponse(in messages: [HarnessMessage]) -> (response: HarnessMessage, completion: HarnessMessage)? {
        guard let response = messages.last(where: {
            $0.type == .assistantOutput && !$0.isThinking && !$0.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        }), let completion = messages.last(where: {
            guard $0.type == .status, $0.seq >= response.seq,
                  $0.payload["session_status"] == nil, $0.payload["exited"] == nil else { return false }
            return $0.payload["turn_complete"]?.boolValue == true
                || $0.payload["awaiting"]?.stringValue == "input"
        }), !messages.contains(where: {
            $0.seq > response.seq && $0.seq <= completion.seq && $0.type == .userInput
        }), !messages.contains(where: {
            $0.seq > completion.seq && ($0.type == .userInput || $0.type == .assistantOutput)
        }) else { return nil }
        return (response, completion)
    }

    public static func generation(_ session: SessionSummary) -> String? {
        guard session.hostRetiredAt == nil,
              session.attention == .done || session.attention == .errored else { return nil }
        if let id = session.attentionID { return "attention:" + id }
        guard let date = session.activityDate else { return nil }
        return "activity:\(date.timeIntervalSince1970)"
    }

    /// Terminal summaries prove process state, not an assistant result. Cache
    /// transcript evidence against the exact summary revision, never a host.
    public func needsResponseEvidence(_ session: SessionSummary) -> Bool {
        requiresResponseEvidence(session) && evidence[session.id]?["revision"] != revision(session)
    }

    /// Rotate bounded attempts so failed or truncated tails cannot starve
    /// another host's newly completed response on subsequent refreshes.
    public func evidenceCandidates(in sessions: [SessionSummary], after sessionID: String?, limit: Int = 4) -> [SessionSummary] {
        let pending = sessions.filter { $0.hostRetiredAt == nil && needsResponseEvidence($0) }
            .sorted { $0.id < $1.id }
        guard limit > 0, !pending.isEmpty else { return [] }
        let start = sessionID.flatMap { id in pending.firstIndex(where: { $0.id > id }) } ?? 0
        return Array((pending[start...] + pending[..<start]).prefix(limit))
    }

    private var evidence: [String: [String: String]] {
        defaults.dictionary(forKey: prefix + "evidence") as? [String: [String: String]] ?? [:]
    }

    private func requiresResponseEvidence(_ session: SessionSummary) -> Bool {
        ["completed", "terminated", "errored"].contains(session.status) || session.attention == .errored
    }

    private func revision(_ session: SessionSummary) -> String {
        "\(session.status)|\(session.attentionID ?? "")|\(session.activityDate?.timeIntervalSince1970 ?? 0)"
    }

    public func recordResponseEvidence(_ session: SessionSummary, messages: [HarnessMessage]) {
        var stored = evidence
        let result = Self.completedResponse(in: messages)
        stored[session.id] = ["revision": revision(session),
                              "generation": result.map { "attention:" + $0.completion.id } ?? ""]
        defaults.set(stored, forKey: prefix + "evidence")
        // Resolve a first-install baseline to the actual completion once known.
        // Later stop/activity revisions must not resurrect that same old result.
        var dismissed = defaults.dictionary(forKey: prefix + "dismissed") as? [String: String] ?? [:]
        if dismissed[session.id] == "baseline:" + revision(session), let result {
            dismissed[session.id] = "attention:" + result.completion.id
            defaults.set(dismissed, forKey: prefix + "dismissed")
        }
    }

    private func resultGeneration(_ session: SessionSummary) -> String? {
        guard Self.generation(session) != nil else { return nil }
        if requiresResponseEvidence(session) {
            guard let value = evidence[session.id], value["revision"] == revision(session),
                  let generation = value["generation"], !generation.isEmpty else { return nil }
            return generation
        }
        return Self.generation(session)
    }

    /// First-install migration: preserve one latest non-terminal result per host for review,
    /// rather than flooding Home with every old finished session. All subsequent
    /// results persist without a timeout. Missing/filtered snapshots never prune.
    public func bootstrap(_ sessions: [SessionSummary]) {
        guard !defaults.bool(forKey: prefix + "bootstrapped") else { return }
        let finished = sessions.filter { Self.generation($0) != nil }
        let newest = Dictionary(grouping: finished, by: \.hostID).values.compactMap {
            $0.filter { !requiresResponseEvidence($0) }
                .max { ($0.activityDate ?? .distantPast) < ($1.activityDate ?? .distantPast) }?.id
        }
        var dismissed = defaults.dictionary(forKey: prefix + "dismissed") as? [String: String] ?? [:]
        for session in finished where !newest.contains(session.id) {
            dismissed[session.id] = resultGeneration(session) ?? "baseline:" + revision(session)
        }
        defaults.set(dismissed, forKey: prefix + "dismissed")
        defaults.set(true, forKey: prefix + "bootstrapped")
    }

    public func unread(in sessions: [SessionSummary]) -> [SessionSummary] {
        let dismissed = defaults.dictionary(forKey: prefix + "dismissed") as? [String: String] ?? [:]
        let read = defaults.dictionary(forKey: prefix + "viewed") as? [String: [String]] ?? [:]
        return sessions.filter { session in
            guard let generation = resultGeneration(session),
                  dismissed[session.id] != generation,
                  dismissed[session.id] != "baseline:" + revision(session) else { return false }
            if read[session.id]?.contains(generation) == true { return false }
            return true
        }.sorted { ($0.activityDate ?? .distantPast) > ($1.activityDate ?? .distantPast) }
    }

    /// Call only for an actual visible, non-thinking assistant response, not for
    /// a loaded transcript or a session destination appearing/disappearing.
    public func viewed(sessionID: String, response: HarnessMessage, messages: [HarnessMessage]) {
        guard response.type == .assistantOutput, !response.isThinking,
              let result = Self.completedResponse(in: messages),
              result.response.seq == response.seq else { return }
        let completion = result.completion
        var generations = ["attention:" + completion.id]
        if let timestamp = completion.timestamp {
            generations.append("activity:\(timestamp.timeIntervalSince1970)")
        }
        var read = defaults.dictionary(forKey: prefix + "viewed") as? [String: [String]] ?? [:]
        read[sessionID] = generations
        defaults.set(read, forKey: prefix + "viewed")
    }

    public func dismiss(_ session: SessionSummary) {
        guard let generation = resultGeneration(session) else { return }
        var dismissed = defaults.dictionary(forKey: prefix + "dismissed") as? [String: String] ?? [:]
        dismissed[session.id] = generation
        defaults.set(dismissed, forKey: prefix + "dismissed")
    }
}
