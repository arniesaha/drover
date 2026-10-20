import DroverKit
import SwiftUI

/// One place for the attention queue, findings, and all-session history.
struct InboxView: View {
    enum Segment: String, CaseIterable, Identifiable {
        case needsYou = "Needs you"
        case insights = "Insights"
        case history = "History"
        var id: String { rawValue }
    }

    let client: DroverClient
    let sessionStore: SessionStore
    let cockpitStore: CockpitStore
    let workStore: WorkAttentionStore
    let attentionStore: InboxAttentionStore
    let onOpenSession: (String) -> Void
    @State private var segment: Segment = .needsYou
    @State private var focusedWorkID: String?

    private var waitingSessions: [SessionSummary] {
        InboxAttentionPresentation.sessionsAwaitingAnswer(sessionStore.snapshot?.sessions ?? [])
    }

    private var needsYouCount: Int {
        InboxAttentionPresentation.count(
            workItems: workStore.items, sessions: sessionStore.snapshot?.sessions ?? [], insights: attentionStore.insights
        )
    }

    var body: some View {
        VStack(spacing: 0) {
            ScreenHeader(
                title: "Inbox",
                subtitle: "Decisions, insights, and session history"
            )
            Picker("Inbox section", selection: $segment) {
                Text("Needs you · \(needsYouCount)").tag(Segment.needsYou)
                Text(Segment.insights.rawValue).tag(Segment.insights)
                Text(Segment.history.rawValue).tag(Segment.history)
            }
            .pickerStyle(.segmented)
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .accessibilityIdentifier("inbox-segments")

            Group {
                switch segment {
                case .needsYou: needsYou
                case .insights: insights
                case .history: HistoryView(client: client, showsNavigationTitle: false)
                }
            }
        }
        .background(DroverColor.bg)
        .toolbar(.hidden, for: .navigationBar)
        .task { await refresh(); openAttentionFromNotification() }
        .onChange(of: workStore.items) { _, _ in openAttentionFromNotification() }
        .onChange(of: NotificationRoute.shared.pendingAttentionRef) { _, _ in openAttentionFromNotification() }
    }

    private var needsYou: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 12) {
                if workStore.isVisible {
                    WorkAttentionList(store: workStore, focusedID: focusedWorkID) { onOpenSession($0) }
                }

                if !waitingSessions.isEmpty {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("Awaiting your answer · \(waitingSessions.count)").font(.headline)
                        ForEach(waitingSessions) { session in
                            Button { onOpenSession(session.id) } label: {
                                SessionRow(
                                    session: session,
                                    hostTitle: hostTitle(for: session),
                                    freshness: sessionStore.freshness()
                                )
                            }
                            .buttonStyle(.plain)
                            .accessibilityIdentifier("inbox-session-\(session.id)")
                        }
                    }
                }

                if !attentionStore.insights.isEmpty {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("Open priority insights · \(attentionStore.insights.count)").font(.headline)
                        ForEach(attentionStore.insights, id: \.findingID) { insight in
                            NavigationLink {
                                InsightDetailView(client: client, store: cockpitStore, summary: insight)
                                    .onDisappear {
                                        Task {
                                            await attentionStore.refresh(
                                                isAvailable: cockpitStore.isInsightsAvailable
                                            )
                                        }
                                    }
                            } label: {
                                InsightFeedCard(insight: insight, state: insight.state)
                            }
                            .buttonStyle(.plain)
                        }
                    }
                }

                if !workStore.isVisible && waitingSessions.isEmpty && attentionStore.insights.isEmpty && !attentionStore.isLoading {
                    StateView(
                        kind: .empty,
                        title: "All caught up",
                        message: "New decisions and priority findings will appear here."
                    )
                    .padding(.vertical, 40)
                }
            }
            .padding(14)
        }
        .accessibilityIdentifier("inbox-needs-you")
        .refreshable { await refresh() }
    }

    private var insights: some View {
        InsightsView(
            client: client, store: cockpitStore,
            showsConfigurationSummary: true, showsNavigationTitle: false
        )
            .accessibilityIdentifier("inbox-insights")
    }

    private func refresh() async {
        await workStore.refresh()
        await attentionStore.refresh(isAvailable: cockpitStore.isInsightsAvailable)
    }

    private func hostTitle(for session: SessionSummary) -> String {
        sessionStore.snapshot?.hosts.first { $0.id == session.hostID }?.title
            ?? session.hostDisplayName
            ?? session.hostID
    }

    private func openAttentionFromNotification() {
        guard let ref = NotificationRoute.shared.pendingAttentionRef else { return }
        segment = .needsYou
        focusedWorkID = workStore.items.first {
            $0.workspaceID == ref.workspaceID && $0.id == ref.id
        }?.id
        _ = NotificationRoute.shared.consumeAttention()
    }
}
