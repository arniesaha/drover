import BackgroundTasks
import DroverKit

/// Owns the `com.arnab.drover.refresh` BGAppRefreshTask lifecycle: register
/// the handler once at app init (BGTaskScheduler requires this before the app
/// finishes launching), then schedule/reschedule a request each time the task
/// runs and each time the app backgrounds.
///
/// The handler rebuilds its own `DroverClient` via `ClientFactory.make(...)`
/// rather than capturing one from the app, because a BGTask can run after the
/// OS has relaunched the process from scratch — there is no live
/// `AppEnvironment` to borrow a client from.
enum BackgroundRefresh {
    static let taskIdentifier = "com.arnab.drover.refresh"

    /// Must be called before `application(_:didFinishLaunchingWithOptions:)`
    /// returns — in this SwiftUI app, from `DroverApp.init()`.
    static func register(notifier: Notifying) {
        BGTaskScheduler.shared.register(forTaskWithIdentifier: taskIdentifier, using: nil) { task in
            // swiftlint:disable:next force_cast — BGTaskScheduler guarantees
            // the task matches the identifier's registered kind.
            handle(task as! BGAppRefreshTask, notifier: notifier)
        }
    }

    /// Schedules (or reschedules) the next refresh, earliest 15 minutes out.
    /// Safe to call repeatedly — a new submit replaces any pending request
    /// for the same identifier.
    static func schedule() {
        Task {
            let config = ClientFactory.make()?.config
            let deadline: Date?
            if let config { deadline = await HubRetryGate.shared.deadline(for: config.baseURL) }
            else { deadline = nil }
            let request = BGAppRefreshTaskRequest(identifier: taskIdentifier)
            request.earliestBeginDate = nextRefreshDate(retryDeadline: deadline)
            try? BGTaskScheduler.shared.submit(request)
        }
    }

    static func nextRefreshDate(now: Date = Date(), retryDeadline: Date?) -> Date {
        max(now.addingTimeInterval(15 * 60), retryDeadline ?? .distantPast)
    }

    private static func handle(_ task: BGAppRefreshTask, notifier: Notifying) {
        // Reschedule immediately so a future refresh is always queued, even
        // if this run gets expired/killed before completing.
        schedule()

        // `BGAppRefreshTask` isn't `Sendable`, but Apple's documented contract
        // is that `setTaskCompleted(success:)` may be called from any queue
        // once the task is done — that's exactly the "unchecked" case this
        // box exists for.
        let box = UncheckedSendableBox(value: task)
        let watcher = AttentionWatcher(notifier: notifier)
        let work = Task {
            var success = false
            if let built = ClientFactory.make() {
                success = await watcher.check(client: built.client)
                schedule()
            }
            box.value.setTaskCompleted(success: success)
        }

        task.expirationHandler = {
            work.cancel()
        }
    }
}

/// Wraps a known-thread-safe-in-practice, non-`Sendable` reference so it can
/// cross into a `Task { ... }` closure. Only introduce this for types whose
/// documented contract makes the unchecked promise true — here,
/// `BGAppRefreshTask.setTaskCompleted(success:)`.
private struct UncheckedSendableBox<Value>: @unchecked Sendable {
    let value: Value
}
