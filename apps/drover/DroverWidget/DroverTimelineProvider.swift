import Foundation
import WidgetKit
import DroverKit

public struct DroverWidgetEntry: TimelineEntry {
    public let date: Date
    public let snapshot: WidgetFleetSnapshot
    public let isPreview: Bool

    public init(date: Date, snapshot: WidgetFleetSnapshot, isPreview: Bool = false) {
        self.date = date
        self.snapshot = snapshot
        self.isPreview = isPreview
    }

    public var isStale: Bool {
        snapshot.isStale(now: date, threshold: 20 * 60)
    }
}

public struct DroverTimelineProvider: TimelineProvider {
    public typealias Entry = DroverWidgetEntry

    public init() {}

    public func placeholder(in context: Context) -> DroverWidgetEntry {
        DroverWidgetEntry(date: Date(), snapshot: .preview(), isPreview: true)
    }

    public func getSnapshot(in context: Context, completion: @escaping (DroverWidgetEntry) -> Void) {
        if context.isPreview {
            completion(DroverWidgetEntry(date: Date(), snapshot: .preview(), isPreview: true))
            return
        }
        let cached = WidgetSnapshotStore.readSnapshot() ?? .preview()
        completion(DroverWidgetEntry(date: Date(), snapshot: cached))
    }

    public func getTimeline(in context: Context, completion: @escaping (Timeline<DroverWidgetEntry>) -> Void) {
        let now = Date()
        let cached = WidgetSnapshotStore.readSnapshot() ?? .preview()
        let sendableCompletion = unsafeBitCast(
            completion,
            to: (@Sendable (Timeline<DroverWidgetEntry>) -> Void).self
        )

        Task {
            // Attempt short background fetch directly from the paired hub if configured
            let freshSnapshot = await Self.fetchFreshSnapshot(fallback: cached, now: now)
            let entry = DroverWidgetEntry(date: now, snapshot: freshSnapshot)

            // Refresh policy: every ~20 minutes within WidgetKit's budget
            let nextRefresh = Calendar.current.date(byAdding: .minute, value: 20, to: now)
                ?? now.addingTimeInterval(20 * 60)
            let timeline = Timeline(entries: [entry], policy: .after(nextRefresh))
            sendableCompletion(timeline)
        }
    }

    private static func fetchFreshSnapshot(fallback: WidgetFleetSnapshot, now: Date) async -> WidgetFleetSnapshot {
        // Read configuration from shared App Group defaults
        guard let groupDefaults = UserDefaults(suiteName: WidgetSnapshotStore.appGroupID),
              let config = ServerConfig.load(defaults: groupDefaults)
        else {
            return fallback
        }

        // Read token from shared keychain access group
        let tokenStore = TokenStore(accessGroup: TokenStore.sharedAccessGroup)
        guard let token = tokenStore.load(), !token.isEmpty else {
            return fallback
        }

        // Ephemeral URLSession with short timeout so timeline never hangs
        let sessionConfig = URLSessionConfiguration.ephemeral
        sessionConfig.timeoutIntervalForRequest = 4.0
        sessionConfig.timeoutIntervalForResource = 6.0
        let session = URLSession(configuration: sessionConfig)

        let client = DroverClient(config: config, token: token, session: session)

        do {
            async let snapshotTask = client.snapshot()
            async let overviewTask = client.cockpitOverview()

            let (harnessSnapshot, overview) = try await (snapshotTask, overviewTask)
            let fresh = WidgetFleetSnapshot.build(
                snapshot: harnessSnapshot,
                accounts: overview.providerCapacity.data ?? [],
                now: now
            )
            // Persist the fresh snapshot back to the shared container
            try? WidgetSnapshotStore.writeSnapshot(fresh)
            return fresh
        } catch {
            // Never block on the network: fall back to the existing snapshot
            return fallback
        }
    }
}
