import DroverKit
import SwiftUI

struct InsightDetailView: View {
    let client: DroverClient
    let store: CockpitStore
    let summary: InsightSummary
    @State private var detail: InsightDetail?
    @State private var loadError: String?
    @State private var showDismiss = false
    @State private var dismissalReason = ""
    @State private var actionMessage: String?
    @State private var checkState = InsightCheckActionState.ready
    @State private var evidenceExpanded = false
    @State private var currentState: InsightState?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                if let detail {
                    let state = currentState ?? detail.finding.state
                    findingHeader(detail.finding, state: state, evidence: detail.evidence)
                    impactSection(detail.finding)
                    actionSection(detail, state: state)
                    remediationSection(detail.finding)
                    evidenceSection(detail.evidence)
                } else if let loadError {
                    ContentUnavailableView(
                        "Insight unavailable", systemImage: "exclamationmark.triangle",
                        description: Text(loadError)
                    )
                } else {
                    ProgressView("Loading insight…")
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 60)
                }
            }
            .padding(14)
        }
        .background(DroverColor.bg)
        .navigationTitle("Insight")
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
        .refreshable { await load() }
        .sheet(isPresented: $showDismiss) { dismissalSheet }
    }

    private func findingHeader(
        _ finding: InsightFinding, state: InsightState, evidence: [InsightEvidence]
    ) -> some View {
        let value = InsightPresentation(insight: finding)
        let statusText = "Status: \(InsightEvidencePresentation.label(for: state.rawValue))"
        let evidenceSummary = evidenceSummaryText(evidence)
        return CockpitCard {
            VStack(alignment: .leading, spacing: 8) {
                FlowLayout(spacing: 7, lineSpacing: 4) {
                    Text(value.severityText).droverText(.marker)
                    Text(statusText)
                        .droverText(.subtitle)
                    Text(value.sourceText).droverText(.subtitle)
                    Text(value.confidenceText).droverText(.subtitle)
                }
                Text(finding.title).droverText(.h1).fixedSize(horizontal: false, vertical: true)
                Text("\(finding.targetType.replacingOccurrences(of: "_", with: " ")) · \(finding.targetID)")
                    .droverText(.mono)
                    .fixedSize(horizontal: false, vertical: true)
                Text(evidenceSummary)
                    .droverText(.nested)
                if let uncertainty = value.uncertaintyText {
                    Text(uncertainty).droverText(.nested)
                }
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("insight-detail-header")
        .accessibilityLabel(InsightDetailHeaderPresentation.accessibilityLabel(
            severity: value.severityText,
            status: statusText,
            confidence: value.confidenceText,
            source: value.sourceText,
            title: finding.title,
            targetID: finding.targetID,
            evidenceSummary: evidenceSummary,
            uncertainty: value.uncertaintyText
        ))
    }

    private func impactSection(_ finding: InsightFinding) -> some View {
        detailSection("Why it matters") {
            Text(finding.impact).droverText(.body).fixedSize(horizontal: false, vertical: true)
        }
    }

    private func evidenceSummaryText(_ evidence: [InsightEvidence]) -> String {
        let latest = evidence.map(\.observedAt).max()
        let count = evidence.count
        let countText = "\(count) observation\(count == 1 ? "" : "s")"
        guard let latest else { return "Evidence: \(countText) · latest observation unavailable" }
        return "Evidence: \(countText) · latest \(latest.formatted(date: .abbreviated, time: .shortened))"
    }

    @ViewBuilder
    private func evidenceSection(_ evidence: [InsightEvidence]) -> some View {
        if !evidence.isEmpty {
            detailSection("Evidence details") {
                DisclosureGroup(
                    "Show evidence details",
                    isExpanded: $evidenceExpanded
                ) {
                    VStack(alignment: .leading, spacing: 12) {
                        ForEach(Array(evidence.enumerated()), id: \.offset) { _, item in
                            VStack(alignment: .leading, spacing: 4) {
                                Text("Source reference: \(item.sourceReference)").droverText(.mono)
                                Text(item.observedAt.formatted(date: .abbreviated, time: .shortened))
                                    .droverText(.subtitle)
                                ForEach(item.fields.keys.sorted(), id: \.self) { key in
                                    Text("\(InsightEvidencePresentation.label(for: key)): \(InsightEvidencePresentation.valueText(item.fields[key]))")
                                        .droverText(.nested)
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                                if let excerpt = item.excerpt {
                                    Text(excerpt)
                                        .droverText(.nested)
                                        .padding(8)
                                        .background(DroverColor.bg, in: RoundedRectangle(cornerRadius: 8))
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    private func remediationSection(_ finding: InsightFinding) -> some View {
        detailSection("Guided remediation") {
            VStack(alignment: .leading, spacing: 9) {
                ForEach(Array(finding.remediation.enumerated()), id: \.offset) { index, step in
                    HStack(alignment: .top, spacing: 9) {
                        Text("\(index + 1)").droverText(.marker)
                        Text(step).droverText(.body).fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        }
    }

    private func actionSection(_ detail: InsightDetail, state: InsightState) -> some View {
        let finding = detail.finding
        let actions = InsightLifecycleActionsPresentation(
            state: state, checkAgainAvailable: detail.actions.checkAgain.available
        )
        return VStack(alignment: .leading, spacing: 10) {
            if actions.canCheckAgain {
                Text("Checking reruns analysis. It does not apply configuration changes.")
                    .droverText(.nested)
                    .foregroundStyle(DroverColor.muted)
                Button {
                    guard checkState.begin() else { return }
                    Task {
                        let accepted = await store.checkInsight(findingID: finding.findingID)
                        checkState.finish(accepted: accepted, error: store.lifecycleError)
                    }
                } label: {
                    Text(checkState.isPending ? "Checking…" : "Check Again (reanalysis)")
                }
                .buttonStyle(.borderedProminent)
                .disabled(checkState.isPending)
                .accessibilityHint("Reruns analysis only; it does not change configuration")
                .accessibilityIdentifier("insight-check-again")

                if let message = checkState.notice {
                    Text(message)
                        .droverText(.nested)
                        .foregroundStyle(checkState.isFailure ? DroverColor.accentHi : DroverColor.muted)
                }
            } else if !detail.actions.checkAgain.available {
                Text(detail.actions.checkAgain.reason ?? "Scoped reanalysis is unavailable.")
                    .droverText(.nested)
                    .foregroundStyle(DroverColor.muted)
            }

            if actions.canAcknowledge || actions.canDismiss {
                HStack {
                    if actions.canAcknowledge {
                        Button("Acknowledge") {
                            Task {
                                if await store.acknowledgeInsight(findingID: finding.findingID) {
                                    currentState = store.state(forFindingID: finding.findingID)
                                        ?? currentState
                                    actionMessage = "Insight acknowledged."
                                }
                            }
                        }
                        .buttonStyle(.bordered)
                    }
                    if actions.canDismiss {
                        Button("Dismiss…") { showDismiss = true }
                            .buttonStyle(.bordered)
                    }
                }
            }
            // Only suppress a duplicate of the check notice when that notice is
            // actually on screen. When the Check Again block is hidden, deduping
            // against it renders the message nowhere at all.
            let checkNoticeVisible = actions.canCheckAgain
            let lifecycleMessage = store.lifecycleError ?? actionMessage
            if let message = lifecycleMessage,
                !(checkNoticeVisible && message == checkState.notice)
            {
                Text(message)
                    .droverText(.nested)
                    .foregroundStyle(store.lifecycleError == nil ? DroverColor.muted : DroverColor.accentHi)
            }
        }
    }

    private func detailSection<Content: View>(
        _ title: String, @ViewBuilder content: () -> Content
    ) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title).droverText(.h3)
            CockpitCard { content() }
        }
    }

    private var dismissalSheet: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("Reason", text: $dismissalReason, axis: .vertical)
                        .lineLimit(3...6)
                        .accessibilityIdentifier("dismissal-reason")
                    if let error = store.lifecycleError {
                        Text(error)
                            .font(.footnote)
                            .foregroundStyle(DroverColor.accentHi)
                    }
                } footer: {
                    Text("A reason is required. The insight may reopen if evidence materially changes.")
                }
            }
            .navigationTitle("Dismiss insight")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { showDismiss = false }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Dismiss") {
                        Task {
                            if await store.dismissInsight(
                                findingID: summary.findingID, reason: dismissalReason
                            ) {
                                currentState = store.state(forFindingID: summary.findingID)
                                    ?? currentState
                                actionMessage = "Insight dismissed."
                                showDismiss = false
                                dismissalReason = ""
                            }
                        }
                    }
                    .disabled(dismissalReason.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }
        }
        .presentationDetents([.medium])
    }

    private func load() async {
        do {
            let loadedDetail = try await client.insightDetail(findingID: summary.findingID)
            detail = loadedDetail
            currentState = loadedDetail.finding.state
            loadError = nil
            // The notice describes the last request, not the data just fetched.
            // Leaving it up lets "Reanalysis queued. Refresh to view the latest
            // result." sit above the refreshed result it was asking for.
            if !checkState.isPending {
                checkState = .ready
            }
        } catch {
            loadError = (error as NSError).localizedDescription
        }
    }
}

private extension InsightCheckActionState {
    var isFailure: Bool {
        if case .failed = self { return true }
        return false
    }
}
