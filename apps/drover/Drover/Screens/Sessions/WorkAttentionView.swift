import SwiftUI
import DroverKit

struct WorkAttentionList: View {
    let store: WorkAttentionStore
    var focusedID: String?
    let openSession: (String) -> Void
    @Environment(\.openURL) private var openURL

    var body: some View {
        if store.isVisible {
            VStack(alignment: .leading, spacing: 10) {
                Text("Work decisions").font(.headline)
                if let message = store.message {
                    Text(message).font(.footnote).foregroundStyle(.secondary)
                }
                if store.truncated { Text("More work needs attention. Open GitHub to inspect all items.") }
                ForEach(store.items) { item in
                    WorkAttentionRow(
                        item: item, stale: !store.isReachable || item.stale,
                        focused: item.id == focusedID,
                        canDecide: { store.canDecide(item, action: $0) },
                        decide: { action in
                            Task {
                                if let url = await store.decide(item, action: action) { openURL(url) }
                            }
                        },
                        openSession: openSession
                    )
                    .id(item.id)
                }
            }
            .accessibilityIdentifier("work-attention-list")
        }
    }
}

struct WorkAttentionRow: View {
    let item: WorkAttentionItem
    let stale: Bool
    let focused: Bool
    let canDecide: (String) -> Bool
    let decide: (String) -> Void
    let openSession: (String) -> Void
    @State private var proposedAction: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(item.title).font(.headline)
            Text(item.repo).font(.body)
            if let category = item.category { Text(category.replacingOccurrences(of: "_", with: " ").capitalized).font(.body) }
            TimelineView(.periodic(from: .now, by: 1)) { context in
                VStack(alignment: .leading, spacing: 10) {
                    Text(item.refreshAge(now: context.date)).font(.footnote)
                    if stale || item.isStale(now: context.date) {
                        Text("Stale. Refresh before deciding.").font(.body)
                    }
                }
            }
            if item.uncertain == true { Text("Outcome uncertain. Inspect the linked source.") }
            if let url = item.githubURL {
                Link("Open GitHub", destination: url).frame(minHeight: 44)
            }
            if let sessionID = item.sessionID {
                Button("Open session") { openSession(sessionID) }.frame(minHeight: 44)
            }
            ForEach(item.actions, id: \.self) { action in
                Button(actionLabel(action)) { proposedAction = action }
                    .disabled(!canDecide(action))
                    .frame(minHeight: 44)
            }
        }
        .fixedSize(horizontal: false, vertical: true)
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(14)
        .background(DroverColor.surface, in: RoundedRectangle(cornerRadius: 14))
        .overlay {
            if focused { RoundedRectangle(cornerRadius: 14).stroke(DroverColor.accentHi, lineWidth: 2) }
        }
        .accessibilityIdentifier("work-attention-\(item.id)")
        .confirmationDialog("Resolve this request?", isPresented: Binding(
            get: { proposedAction != nil }, set: { if !$0 { proposedAction = nil } }
        ), titleVisibility: .visible) {
            if let action = proposedAction {
                Button(actionLabel(action)) { proposedAction = nil; decide(action) }
            }
            Button("Cancel", role: .cancel) { proposedAction = nil }
        } message: {
            Text("The source and current permissions will be refreshed before this decision.")
        }
    }

    private func actionLabel(_ action: String) -> String {
        switch action {
        case "reject": "Request revision"
        case "approve": "Approve request"
        case "retry": "Retry after review"
        default: action.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }
}
