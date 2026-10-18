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

        public init(identifier: String, sessionID: String?) {
            self.identifier = identifier
            self.sessionID = sessionID
        }
    }

    public static func identifiersToClear(
        sessionID: String, delivered: [Delivered]
    ) -> [String] {
        delivered.compactMap { item in
            item.sessionID == sessionID ? item.identifier : nil
        }
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
                )
            )
        }
        let identifiers = identifiersToClear(sessionID: sessionID, delivered: records)
        guard !identifiers.isEmpty else { return }
        center.removeDeliveredNotifications(withIdentifiers: identifiers)
        center.removePendingNotificationRequests(withIdentifiers: identifiers)
    }
}
