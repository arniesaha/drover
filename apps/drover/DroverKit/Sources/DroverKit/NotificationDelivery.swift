import Foundation
import UserNotifications

/// Notification-center details shared by remote and local session alerts.
/// Keeping the matching rule pure makes it testable without asking iOS for
/// notification permission.
public enum SessionNotificationDelivery {
    public static let openActionIdentifier = "DROVER_OPEN_SESSION"

    public struct Delivered: Sendable, Equatable {
        public let identifier: String
        public let sessionID: String?
        public let attentionRef: WorkAttentionRef?
        public let categoryIdentifier: String

        public init(
            identifier: String,
            sessionID: String?,
            attentionRef: WorkAttentionRef? = nil,
            categoryIdentifier: String = ""
        ) {
            self.identifier = identifier
            self.sessionID = sessionID
            self.attentionRef = attentionRef
            self.categoryIdentifier = categoryIdentifier
        }
    }

    public static func identifiersToClear(
        sessionID: String, delivered: [Delivered]
    ) -> [String] {
        delivered.compactMap { item in
            item.sessionID == sessionID ? item.identifier : nil
        }
    }

    /// An unanswered-session alert becomes misleading as soon as the hub's
    /// next snapshot says that session is no longer actionable. Completion
    /// and failure alerts have their own categories and remain untouched.
    public static func identifiersToClearForResolvedAwaiting(
        activeSessionIDs: Set<String>, delivered: [Delivered], pending: [Delivered] = []
    ) -> [String] {
        identifiers(in: delivered + pending) { item in
            guard item.categoryIdentifier == SessionNotificationKind.input.rawValue,
                  let sessionID = item.sessionID,
                  !activeSessionIDs.contains(sessionID) else { return nil }
            return item.identifier
        }
    }

    /// A complete workspace response is authoritative only for its own
    /// workspace. A work alert carries `attention_ref`, unlike a session
    /// alert, so it can be retired without touching other input categories.
    public static func identifiersToClearForResolvedWorkAttention(
        activeAttentionRefs: Set<WorkAttentionRef>, workspaceIDs: Set<String>,
        delivered: [Delivered], pending: [Delivered] = []
    ) -> [String] {
        identifiers(in: delivered + pending) { item in
            guard item.categoryIdentifier == SessionNotificationKind.input.rawValue,
                  let ref = item.attentionRef,
                  workspaceIDs.contains(ref.workspaceID),
                  !activeAttentionRefs.contains(ref) else { return nil }
            return item.identifier
        }
    }

    private static func identifiers(
        in records: [Delivered], where include: (Delivered) -> String?
    ) -> [String] {
        Array(Set(records.compactMap(include))).sorted()
    }

    /// Register only a safe Open action. Approval responses stay in the app:
    /// notification actions cannot reliably carry the current approval token.
    public static func registerCategories(center: UNUserNotificationCenter = .current()) {
        let open = UNNotificationAction(
            identifier: openActionIdentifier,
            title: "Open",
            options: [.foreground]
        )
        let categories = ["input", "finished", "failed", "progress"].map {
            UNNotificationCategory(
                identifier: $0, actions: [open], intentIdentifiers: [], options: []
            )
        }
        center.setNotificationCategories(Set(categories))
    }

    /// Remove any delivered alert for a session after the user opens it. APNs
    /// request identifiers are system-owned, so enumerate and inspect the
    /// payload rather than assuming the session ID is the request ID.
    public static func clear(sessionID: String, center: UNUserNotificationCenter = .current()) async {
        let delivered = await center.deliveredNotifications()
        let records = delivered.map { notification in
            Delivered(
                identifier: notification.request.identifier,
                sessionID: NotificationRoute.sessionID(
                    userInfo: notification.request.content.userInfo,
                    requestIdentifier: notification.request.identifier
                ),
                attentionRef: NotificationRoute.attentionRef(userInfo: notification.request.content.userInfo),
                categoryIdentifier: notification.request.content.categoryIdentifier
            )
        }
        let identifiers = identifiersToClear(sessionID: sessionID, delivered: records)
        guard !identifiers.isEmpty else { return }
        center.removeDeliveredNotifications(withIdentifiers: identifiers)
        center.removePendingNotificationRequests(withIdentifiers: identifiers)
    }

    /// Clear only delivered/pending question-or-approval alerts that are no
    /// longer active in a successful snapshot.
    public static func clearResolvedAwaiting(
        activeSessionIDs: Set<String>, center: UNUserNotificationCenter = .current()
    ) async {
        let delivered = await records(delivered: center)
        let pending = await records(pending: center)
        let identifiers = identifiersToClearForResolvedAwaiting(
            activeSessionIDs: activeSessionIDs, delivered: delivered, pending: pending
        )
        guard !identifiers.isEmpty else { return }
        center.removeDeliveredNotifications(withIdentifiers: identifiers)
        center.removePendingNotificationRequests(withIdentifiers: identifiers)
    }

    /// Retire local work-action alerts after a complete, non-stale workspace
    /// response. Both center stores matter: a request can be pending without
    /// ever having appeared in delivered notifications.
    public static func clearResolvedWorkAttention(
        activeAttentionRefs: Set<WorkAttentionRef>, workspaceIDs: Set<String>,
        center: UNUserNotificationCenter = .current()
    ) async {
        guard !workspaceIDs.isEmpty else { return }
        let delivered = await records(delivered: center)
        let pending = await records(pending: center)
        let identifiers = identifiersToClearForResolvedWorkAttention(
            activeAttentionRefs: activeAttentionRefs, workspaceIDs: workspaceIDs,
            delivered: delivered, pending: pending
        )
        guard !identifiers.isEmpty else { return }
        center.removeDeliveredNotifications(withIdentifiers: identifiers)
        center.removePendingNotificationRequests(withIdentifiers: identifiers)
    }

    private static func records(delivered center: UNUserNotificationCenter) async -> [Delivered] {
        await center.deliveredNotifications().map(record)
    }

    private static func records(pending center: UNUserNotificationCenter) async -> [Delivered] {
        await center.pendingNotificationRequests().map(record)
    }

    private static func record(_ notification: UNNotificationRequest) -> Delivered {
        Delivered(
            identifier: notification.identifier,
            sessionID: NotificationRoute.sessionID(
                userInfo: notification.content.userInfo, requestIdentifier: notification.identifier
            ),
            attentionRef: NotificationRoute.attentionRef(userInfo: notification.content.userInfo),
            categoryIdentifier: notification.content.categoryIdentifier
        )
    }

    private static func record(_ notification: UNNotification) -> Delivered {
        record(notification.request)
    }
}
