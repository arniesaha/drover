import Foundation

/// Keys shared by the two kinds of alert this app can show.
///
/// A "needs you" alert reaches the phone either as an APNs push from the hub
/// (app backgrounded) or as a local notification from `LocalNotifier` (app
/// running). Both carry the session id under the same key so tapping either
/// resolves through one code path rather than two that drift.
public enum NotificationPayloadKey {
    public static let attentionRef = "attention_ref"
    /// Matches the `session_id` the server puts in its APNs payload.
    public static let sessionID = "session_id"
    public static let sessionIDs = "session_ids"
}

/// Whether the hub is currently announcing "needs you" over APNs.
///
/// Once a device token is registered, the hub sends a push for *every*
/// awaiting transition — backgrounded or not. Anything the app announces
/// locally on top of that is a second alert for the same event, which is
/// exactly what it looked like: the hub's push, and then a generic
/// "<harness> needs you" from `AttentionWatcher`.
///
/// Kept in `UserDefaults` rather than in an object because the BGTask path
/// runs in a process the OS may have relaunched from scratch, with no live
/// app state to consult.
public enum PushRegistration {
    static let key = "drover.push.hubAnnounces"

    public static func setActive(_ active: Bool, in store: UserDefaults = .standard) {
        store.set(active, forKey: key)
    }

    /// Defaults to false, so a device that has never registered keeps the
    /// local alerts it has always had.
    public static func isActive(in store: UserDefaults = .standard) -> Bool {
        store.bool(forKey: key)
    }
}

public enum NotificationMode: String, CaseIterable, Codable, Sendable {
    case all, action, digest

    public var label: String {
        switch self {
        case .all: "All updates"
        case .action: "Action needed only"
        case .digest: "Daily digest"
        }
    }

    public static func saved(in store: UserDefaults = .standard) -> Self {
        Self(rawValue: store.string(forKey: "drover.push.mode") ?? "") ?? .action
    }

    public func save(in store: UserDefaults = .standard) {
        store.set(rawValue, forKey: "drover.push.mode")
    }
}

/// Where a tapped notification puts the session it was about, until a screen
/// is ready to navigate there.
///
/// A tap can arrive before any view exists — from a cold launch, iOS delivers
/// the response while the app is still building its first scene — so the id
/// has to be parked somewhere observable rather than handed straight to a
/// view that may not be listening yet.
@MainActor
@Observable
public final class NotificationRoute {
    public static let shared = NotificationRoute()

    /// Set by the notification delegate, cleared by whoever navigates.
    public private(set) var pendingAttentionRef: WorkAttentionRef?
    public private(set) var pendingSessionID: String?
    public private(set) var pendingSessionIDs: [String]?

    public init() {}

    public func open(attentionRef: WorkAttentionRef) {
        pendingSessionID = nil
        pendingSessionIDs = nil
        pendingAttentionRef = attentionRef
    }

    public func consumeAttention() -> WorkAttentionRef? {
        defer { pendingAttentionRef = nil }
        return pendingAttentionRef
    }

    public nonisolated static func attentionRef(userInfo: [AnyHashable: Any]) -> WorkAttentionRef? {
        guard let value = userInfo[NotificationPayloadKey.attentionRef] as? [String: String],
              let workspace = value["workspace_id"], !workspace.isEmpty,
              let id = value["id"], !id.isEmpty else { return nil }
        return WorkAttentionRef(workspaceID: workspace, id: id)
    }

    public func open(sessionID: String) {
        let trimmed = sessionID.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        pendingAttentionRef = nil
        pendingSessionIDs = nil
        pendingSessionID = trimmed
    }

    public func open(sessionIDs: [String]) {
        let ids = Array(Set(sessionIDs.map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty })).sorted()
        guard !ids.isEmpty else { return }
        pendingAttentionRef = nil
        pendingSessionID = nil
        pendingSessionIDs = ids
    }

    public func consumeSummary() -> [String]? {
        defer { pendingSessionIDs = nil }
        return pendingSessionIDs
    }

    public nonisolated static func sessionIDs(userInfo: [AnyHashable: Any]) -> [String]? {
        guard userInfo[NotificationPayloadKey.attentionRef] == nil else { return nil }
        guard let ids = userInfo[NotificationPayloadKey.sessionIDs] as? [String] else { return nil }
        let cleaned = Array(Set(ids.map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty })).sorted()
        return cleaned.isEmpty ? nil : cleaned
    }

    /// Take the pending id, if any, leaving nothing behind — a tap must not
    /// re-navigate every time the sessions list refreshes.
    public func consume() -> String? {
        defer { pendingSessionID = nil }
        return pendingSessionID
    }

    /// Pull the session id out of a notification's `userInfo`, falling back to
    /// the request identifier.
    ///
    /// `LocalNotifier` uses the session id as the request identifier, so that
    /// fallback keeps notifications that were already scheduled before this
    /// shipped tappable rather than inert.
    /// `nonisolated` deliberately: this is a pure parse with no state, and the
    /// notification delegate calls it before hopping to the main actor.
    public nonisolated static func sessionID(
        userInfo: [AnyHashable: Any], requestIdentifier: String
    ) -> String? {
        if userInfo[NotificationPayloadKey.attentionRef] != nil ||
            userInfo[NotificationPayloadKey.sessionIDs] != nil { return nil }
        if let value = userInfo[NotificationPayloadKey.sessionID] as? String,
           !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return value
        }
        let identifier = requestIdentifier.trimmingCharacters(in: .whitespacesAndNewlines)
        return identifier.isEmpty ? nil : identifier
    }
}

/// Foreground banners are redundant only while their own chat is visible.
@MainActor @Observable
public final class NotificationVisibility {
    public static let shared = NotificationVisibility()
    public var isForeground = false
    public private(set) var visibleSessionID: String?
    private var owner: UUID?

    public init() {}

    public func show(sessionID: String, owner: UUID) {
        self.owner = owner
        visibleSessionID = sessionID
    }

    public func hide(owner: UUID) {
        guard self.owner == owner else { return }
        self.owner = nil
        visibleSessionID = nil
    }

    public nonisolated static func shouldSuppress(
        sessionID: String?, visibleSessionID: String?, isForeground: Bool
    ) -> Bool {
        isForeground && sessionID != nil && sessionID == visibleSessionID
    }

    public func shouldSuppress(sessionID: String?) -> Bool {
        Self.shouldSuppress(sessionID: sessionID, visibleSessionID: visibleSessionID,
                            isForeground: isForeground)
    }
}
