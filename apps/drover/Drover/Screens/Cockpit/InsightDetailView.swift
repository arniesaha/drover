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

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                if let detail {
                    findingHeader(detail.finding)
                    impactSection(detail.finding)
                    evidenceSection(detail.evidence)
                    remediationSection(detail.finding)
                    actionSection(detail)
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

    private func findingHeader(_ finding: InsightFinding) -> some View {
        let value = InsightPresentation(insight: finding)
        return CockpitCard {
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 7) {
                    Text(value.severityText).droverText(.marker)
                    Text(value.sourceText).droverText(.subtitle)
                    Text(value.confidenceText).droverText(.subtitle)
                }
                Text(finding.title).droverText(.h1).fixedSize(horizontal: false, vertical: true)
                Text("\(finding.targetType.replacingOccurrences(of: "_", with: " ")) · \(finding.targetID)")
                    .droverText(.mono)
                    .fixedSize(horizontal: false, vertical: true)
                if let uncertainty = value.uncertaintyText {
                    Text(uncertainty).droverText(.nested)
                }
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(value.severityText), \(value.confidenceText), \(value.sourceText), \(finding.title), target \(finding.targetID)")
    }

    private func impactSection(_ finding: InsightFinding) -> some View {
        detailSection("Why it matters") {
            Text(finding.impact).droverText(.body).fixedSize(horizontal: false, vertical: true)
        }
    }

    @ViewBuilder
    private func evidenceSection(_ evidence: [InsightEvidence]) -> some View {
        if !evidence.isEmpty {
            detailSection("Evidence") {
                VStack(alignment: .leading, spacing: 12) {
                    ForEach(Array(evidence.enumerated()), id: \.offset) { _, item in
                        VStack(alignment: .leading, spacing: 4) {
                            Text(item.sourceReference).droverText(.mono)
                            Text(item.observedAt.formatted(date: .abbreviated, time: .shortened))
                                .droverText(.subtitle)
                            ForEach(item.fields.keys.sorted(), id: \.self) { key in
                                Text("\(key.replacingOccurrences(of: "_", with: " ").capitalized): \(String(describing: item.fields[key]!))")
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

    private func actionSection(_ detail: InsightDetail) -> some View {
        let finding = detail.finding
        return VStack(alignment: .leading, spacing: 10) {
            if let message = actionMessage ?? store.lifecycleError {
                Text(message)
                    .droverText(.nested)
                    .foregroundStyle(store.lifecycleError == nil ? DroverColor.muted : DroverColor.accentHi)
            }
            Button("Check Again (reanalysis)") {
                Task {
                    actionMessage = await store.checkInsight(findingID: finding.findingID)
                        ? "Reanalysis queued."
                        : nil
                }
            }
            .buttonStyle(.borderedProminent)
            .disabled(!detail.actions.checkAgain.available)
            .accessibilityHint("Reruns analysis only; it does not change configuration")
            .accessibilityIdentifier("insight-check-again")

            if !detail.actions.checkAgain.available {
                Text(detail.actions.checkAgain.reason ?? "Scoped reanalysis is unavailable.")
                    .droverText(.nested)
                    .foregroundStyle(DroverColor.muted)
            }

            HStack {
                Button("Acknowledge") {
                    Task {
                        actionMessage = await store.acknowledgeInsight(findingID: finding.findingID)
                            ? "Insight acknowledged."
                            : nil
                    }
                }
                .buttonStyle(.bordered)
                Button("Dismiss…") { showDismiss = true }
                    .buttonStyle(.bordered)
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
            detail = try await client.insightDetail(findingID: summary.findingID)
            loadError = nil
        } catch {
            loadError = (error as NSError).localizedDescription
        }
    }
}
