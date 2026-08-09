import DroverKit
import SwiftUI

struct ContentAnalysisSettings: View {
    private enum Mode: String, CaseIterable, Identifiable {
        case disabled
        case local
        case cloud

        var id: Self { self }
        var title: String { rawValue.capitalized }
    }

    @State private var store: CockpitStore
    @State private var selectedMode: Mode = .disabled
    @State private var disclosureAccepted = false
    @State private var showRevokeConfirmation = false
    @State private var showPurgeConfirmation = false
    @State private var didLoadStatus = false

    init(client: DroverClient) {
        _store = State(initialValue: CockpitStore(client: client))
    }

    var body: some View {
        Section("Content analysis") {
            Picker("Analysis backend", selection: $selectedMode) {
                ForEach(Mode.allCases) { mode in
                    Text(mode.title).tag(mode)
                }
            }
            .pickerStyle(.segmented)
            .accessibilityIdentifier("content-analysis-mode")

            modeDescription

            if selectedMode == .cloud {
                Text(CockpitStore.cloudDisclosureMessage)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("content-cloud-disclosure")

                Toggle("I understand this content may leave this device", isOn: $disclosureAccepted)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("content-cloud-disclosure-acceptance")
            }

            if selectedMode != .disabled {
                Button(enableButtonTitle) {
                    Task {
                        _ = await store.enableContentAnalysis(
                            backend: selectedMode == .local ? .local : .cloud,
                            disclosureAccepted: disclosureAccepted
                        )
                    }
                }
                .disabled(
                    store.isUpdatingContentConsent
                        || (selectedMode == .cloud && !disclosureAccepted)
                )
                .accessibilityIdentifier("content-analysis-enable")
            }

            if let error = store.contentStatusError ?? store.contentConsentError {
                operationMessage(error, isError: true)
            } else if let status = store.contentAnalysisStatus {
                operationMessage(statusDescription(status), isError: false)
            }

            if store.contentAnalysisStatus?.enabled == true {
                Button("Stop future model analysis…", role: .destructive) {
                    showRevokeConfirmation = true
                }
                .disabled(store.isRevokingContentAnalysis)
                .accessibilityIdentifier("content-analysis-revoke")
            }

            Button("Purge retained excerpts…", role: .destructive) {
                showPurgeConfirmation = true
            }
            .disabled(store.isPurgingContentExcerpts)
            .accessibilityIdentifier("content-excerpts-purge")

            if let error = store.contentRevocationError {
                operationMessage("Could not stop analysis: \(error)", isError: true)
            }
            if let error = store.contentPurgeError {
                operationMessage("Could not purge excerpts: \(error)", isError: true)
            } else if let count = store.purgedExcerptCount {
                operationMessage("Purged \(count) retained excerpt\(count == 1 ? "" : "s").", isError: false)
                    .accessibilityIdentifier("content-excerpts-purge-status")
            }
        }
        .task {
            guard !didLoadStatus else { return }
            didLoadStatus = true
            await store.loadContentAnalysisStatus()
            syncSelectionFromStatus()
        }
        .onChange(of: store.contentAnalysisStatus) { _, _ in
            syncSelectionFromStatus()
        }
        .confirmationDialog(
            "Stop content analysis?",
            isPresented: $showRevokeConfirmation,
            titleVisibility: .visible
        ) {
            Button("Stop future analysis", role: .destructive) {
                Task { _ = await store.revokeContentAnalysis() }
            }
        } message: {
            Text(CockpitStore.revokeConfirmationMessage)
        }
        .confirmationDialog(
            "Purge retained excerpts?",
            isPresented: $showPurgeConfirmation,
            titleVisibility: .visible
        ) {
            Button("Purge all excerpts", role: .destructive) {
                Task { _ = await store.purgeContentExcerpts() }
            }
        } message: {
            Text(CockpitStore.purgeConfirmationMessage)
        }
    }

    @ViewBuilder
    private var modeDescription: some View {
        switch selectedMode {
        case .disabled:
            Text("Content-sensitive model analysis is off. Deterministic local checks still run.")
                .font(.footnote)
                .foregroundStyle(.secondary)
        case .local:
            Label("Recommended · analyzed on this device", systemImage: "checkmark.shield")
                .font(.footnote)
                .foregroundStyle(.secondary)
        case .cloud:
            Label("External model provider", systemImage: "network")
                .font(.footnote)
                .foregroundStyle(.secondary)
        }
    }

    private var enableButtonTitle: String {
        store.isUpdatingContentConsent
            ? "Saving…"
            : "Enable \(selectedMode.title.lowercased()) analysis"
    }

    private func statusDescription(_ status: ContentAnalysisStatus) -> String {
        if status.enabled {
            return "Enabled · \(status.backend.rawValue.capitalized) · "
                + "\(status.pendingModelJobs) pending model job\(status.pendingModelJobs == 1 ? "" : "s")"
        }
        return "Disabled · existing findings remain available"
    }

    private func operationMessage(_ message: String, isError: Bool) -> some View {
        Text(message)
            .font(.footnote)
            .foregroundStyle(isError ? Color.red : Color.secondary)
            .fixedSize(horizontal: false, vertical: true)
    }

    private func syncSelectionFromStatus() {
        guard let status = store.contentAnalysisStatus else { return }
        selectedMode = status.enabled
            ? (status.backend == .local ? .local : .cloud)
            : .disabled
        disclosureAccepted = status.externalDisclosureAccepted
    }
}
