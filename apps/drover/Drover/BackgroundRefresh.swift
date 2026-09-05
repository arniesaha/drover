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
    private static let activeWorkLock = NSLock()
    nonisolated(unsafe) private static var activeWork: [UUID: Task<Void, Never>] = [:]

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
        schedule(gate: .shared) { request in
            try? BGTaskScheduler.shared.submit(request)
        }
    }

    /// The injectable submit boundary keeps the background-demo gate testable
    /// without registering a real BGTask or touching the system scheduler.
    static func schedule(
        gate: DemoActivityGate,
        submit: (BGAppRefreshTaskRequest) -> Void
    ) {
        guard !gate.isActive else { return }
        let request = BGAppRefreshTaskRequest(identifier: taskIdentifier)
        request.earliestBeginDate = Date(timeIntervalSinceNow: 15 * 60)
        submit(request)
    }

    /// Runs only the client-construction and watcher portion of a refresh.
    /// Tests inject both closures to prove no client or request work starts
    /// while demo mode is active; the production handler uses the same path.
    @MainActor
    static func performWork(
        gate: DemoActivityGate = .shared,
        makeClient: () -> DroverClient? = { ClientFactory.make()?.client },
        check: (DroverClient) async -> Void = { client in
            await AttentionWatcher(notifier: LocalNotifier()).check(client: client)
        }
    ) async -> Bool {
        guard !gate.isActive, !Task.isCancelled else { return false }
        guard let client = makeClient() else { return false }
        // The gate is checked again after construction, because a queued task
        // can race entry into the demo while ClientFactory is resolving state.
        guard !gate.isActive, !Task.isCancelled else { return false }
        await check(client)
        return !gate.isActive && !Task.isCancelled
    }

    /// Entry into the demo cancels queued handler work. In-flight HTTP that
    /// began before the transition is cancelled; `performWork` prevents a
    /// task that has not yet reached its watcher from starting one afterward.
    static func cancelActiveWorkForDemo() {
        let work = activeWorkLock.withLock { () -> [Task<Void, Never>] in
            let work = Array(activeWork.values)
            activeWork.removeAll()
            return work
        }
        work.forEach { $0.cancel() }
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
        let workID = UUID()
        // The task can complete immediately when the demo is active or no
        // client exists. Keep creation and insertion under the same lock so
        // its defer cannot remove before this registry has recorded it.
        let work = activeWorkLock.withLock { () -> Task<Void, Never> in
            let work = Task {
                defer {
                    activeWorkLock.withLock { activeWork.removeValue(forKey: workID) }
                }
                let success = await performWork(
                    check: { client in
                        await AttentionWatcher(notifier: notifier).check(client: client)
                    }
                )
                box.value.setTaskCompleted(success: success)
            }
            activeWork[workID] = work
            return work
        }
        _ = work

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
