import Foundation

/// Keys shared by the two kinds of alert this app can show.
///
/// A "needs you" alert reaches the phone either as an APNs push from the hub
/// (app backgrounded) or as a local notification from `LocalNotifier` (app
/// running). Both carry the session id under the same key so tapping either
/// resolves through one code path rather than two that drift.
public enum NotificationPayloadKey {
    /// Matches the `session_id` the server puts in its APNs payload.
    public static let sessionID = "session_id"
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
    public private(set) var pendingSessionID: String?

    public init() {}

    public func open(sessionID: String) {
        let trimmed = sessionID.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        pendingSessionID = trimmed
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
        if let value = userInfo[NotificationPayloadKey.sessionID] as? String,
           !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return value
        }
        let identifier = requestIdentifier.trimmingCharacters(in: .whitespacesAndNewlines)
        return identifier.isEmpty ? nil : identifier
    }
}
