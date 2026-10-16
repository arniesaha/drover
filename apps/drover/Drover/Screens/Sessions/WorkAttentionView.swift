import SwiftUI
import DroverKit

struct WorkAttentionList: View {
    let store: WorkAttentionStore
    var focusedID: String?
    let openSession: (String) -> Void
    @Environment(\.openURL) private var openURL
    @State private var isShowingAll = false

    var body: some View {
        if store.isVisible {
            VStack(alignment: .leading, spacing: 8) {
                Text("Work decisions · \(store.items.count)").font(.headline)
                if let message = store.message {
                    Text(message).font(.footnote).foregroundStyle(.secondary)
                }
                if store.truncated {
                    Text("More work needs attention. Open GitHub to inspect all items.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
                ForEach(WorkAttentionListPresentation.displayedItems(store.items, isExpanded: isShowingAll)) { item in
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
                if WorkAttentionListPresentation.needsExpansionControl(for: store.items) {
                    Button(isShowingAll ? "Show less" : "Show all \(store.items.count)") {
                        isShowingAll.toggle()
                    }
                    .font(.footnote.weight(.semibold))
                    .accessibilityLabel(isShowingAll ? "Show fewer work decisions" : "Show all \(store.items.count) work decisions")
                }
            }
            .accessibilityIdentifier("work-attention-list")
            .onAppear { revealFocusedItem() }
            .onChange(of: focusedID) { _, _ in revealFocusedItem() }
            .onChange(of: store.items) { _, _ in revealFocusedItem() }
        }
    }

    private func revealFocusedItem() {
        if WorkAttentionListPresentation.isFocusedItemHidden(
            focusedID, in: store.items, isExpanded: isShowingAll
        ) {
            isShowingAll = true
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
    @Environment(\.openURL) private var openURL
    @State private var proposedAction: String?

    var body: some View {
        TimelineView(.periodic(from: .now, by: 1)) { context in
            let isStaleAtTime = stale || item.isStale(now: context.date)
            Button {
                if let url = item.githubURL { openURL(url) }
            } label: {
                HStack(spacing: 10) {
                    VStack(alignment: .leading, spacing: 3) {
                        Text(item.rowLabel)
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(.primary)
                        HStack(spacing: 4) {
                            Text(item.repo).lineLimit(1)
                            Text("·")
                            Text(item.refreshAge(now: context.date)).lineLimit(1)
                            if isStaleAtTime {
                                Text("Stale")
                                    .font(.caption2.weight(.semibold))
                                    .foregroundStyle(DroverColor.warn)
                                    .padding(.horizontal, 5)
                                    .padding(.vertical, 2)
                                    .overlay { Capsule().stroke(DroverColor.warn, lineWidth: 1) }
                            }
                            if item.uncertain == true {
                                Text("Uncertain")
                                    .font(.caption2.weight(.semibold))
                                    .foregroundStyle(DroverColor.accentHi)
                                    .padding(.horizontal, 5)
                                    .padding(.vertical, 2)
                                    .overlay { Capsule().stroke(DroverColor.accentHi, lineWidth: 1) }
                            }
                        }
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                    }
                    Spacer(minLength: 0)
                    Image(systemName: "chevron.right")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(DroverColor.faint)
                        .accessibilityHidden(true)
                }
            }
            .buttonStyle(.plain)
            .opacity(isStaleAtTime ? 0.68 : 1)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.vertical, 8)
            .padding(.horizontal, 12)
            .background(DroverColor.surface, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
            .overlay {
                if focused { RoundedRectangle(cornerRadius: 10, style: .continuous).stroke(DroverColor.accentHi, lineWidth: 2) }
            }
            .accessibilityLabel(accessibilityLabel(isStale: isStaleAtTime))
            .accessibilityHint(item.githubURL == nil ? "GitHub link unavailable" : "Opens in GitHub")
            .accessibilityIdentifier("work-attention-\(item.id)")
            .contextMenu {
                if let sessionID = item.sessionID {
                    Button("Open session") { openSession(sessionID) }
                }
                ForEach(item.actions, id: \.self) { action in
                    Button(actionLabel(action)) { proposedAction = action }
                        .disabled(!canDecide(action))
                }
            }
            .accessibilityActions {
                if let sessionID = item.sessionID {
                    Button("Open session") { openSession(sessionID) }
                }
                ForEach(item.actions, id: \.self) { action in
                    Button(actionLabel(action)) {
                        guard canDecide(action) else { return }
                        proposedAction = action
                    }
                }
            }
        }
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

    private func accessibilityLabel(isStale: Bool) -> String {
        var label = "\(item.rowLabel), \(item.repo)"
        if isStale { label += ", stale" }
        if item.uncertain == true { label += ", uncertain" }
        return label
    }
}
