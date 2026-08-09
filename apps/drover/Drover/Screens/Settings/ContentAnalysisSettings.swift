import DroverKit
import SwiftUI

struct ContentAnalysisSettings: View {
    @State private var store: CockpitStore
    @State private var selectionState = ContentAnalysisSelectionState()
    @State private var showRevokeConfirmation = false
    @State private var showPurgeConfirmation = false
    @State private var didLoadStatus = false

    init(client: DroverClient) {
        _store = State(initialValue: CockpitStore(client: client))
    }

    var body: some View {
        Section("Content analysis") {
            Picker("Analysis backend", selection: modeBinding) {
                ForEach(ContentAnalysisMode.allCases) { mode in
                    Text(mode.title).tag(mode)
                }
            }
            .pickerStyle(.segmented)
            .disabled(store.isContentConsentOperationInProgress)
            .accessibilityIdentifier("content-analysis-mode")

            modeDescription

            if selectionState.displayedMode == .cloud {
                Text(CockpitStore.cloudDisclosureMessage)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("content-cloud-disclosure")

                Toggle(
                    "I understand this content may leave this device",
                    isOn: $selectionState.disclosureAccepted
                )
                    .fixedSize(horizontal: false, vertical: true)
                    .disabled(store.isContentConsentOperationInProgress)
                    .accessibilityIdentifier("content-cloud-disclosure-acceptance")
            }

            if selectionState.displayedMode != .disabled {
                Button(enableButtonTitle) {
                    Task {
                        _ = await store.enableContentAnalysis(
                            backend: selectionState.displayedMode == .local ? .local : .cloud,
                            disclosureAccepted: selectionState.disclosureAccepted
                        )
                    }
                }
                .disabled(
                    store.isContentConsentOperationInProgress
                        || (selectionState.displayedMode == .cloud
                            && !selectionState.disclosureAccepted)
                )
                .accessibilityIdentifier("content-analysis-enable")
            }

            if let error = store.contentStatusError ?? store.contentConsentError {
                operationMessage(error, isError: true)
            } else if let status = store.contentAnalysisStatus {
                operationMessage(statusDescription(status), isError: false)
            }

            propagationWarning

            if store.contentAnalysisStatus?.enabled == true {
                Button("Stop future model analysis…", role: .destructive) {
                    showRevokeConfirmation = true
                }
                .disabled(store.isContentConsentOperationInProgress)
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
        .onChange(of: store.isContentConsentOperationInProgress) { wasRunning, isRunning in
            if wasRunning, !isRunning {
                syncSelectionFromStatus()
            }
        }
        .onChange(of: showRevokeConfirmation) { wasPresented, isPresented in
            if wasPresented, !isPresented, store.contentAnalysisStatus?.enabled == true {
                selectionState.cancelRevocation()
            }
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
        switch selectionState.displayedMode {
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
            : "Enable \(selectionState.displayedMode.title.lowercased()) analysis"
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

    @ViewBuilder
    private var propagationWarning: some View {
        if let status = store.contentAnalysisStatus,
           let outcome = currentPropagationOutcome,
           outcome != .complete {
            let value = ContentAnalysisPropagationPresentation(
                status: status, outcome: outcome
            )
            VStack(alignment: .leading, spacing: 6) {
                Label(value.title, systemImage: "exclamationmark.triangle.fill")
                    .font(.headline)
                Text(
                    "Central consent is \(status.enabled ? "enabled" : "disabled"), "
                        + "but these hosts did not confirm the same state:"
                )
                .font(.footnote)
                ForEach(value.hostLines, id: \.self) { line in
                    Text(line).font(.footnote.monospaced())
                }
                Button("Retry fleet propagation") {
                    Task { _ = await store.retryContentAnalysisPropagation() }
                }
                .disabled(store.isContentConsentOperationInProgress)
                .buttonStyle(.bordered)
                .accessibilityIdentifier("content-analysis-propagation-retry")
            }
            .foregroundStyle(outcome == .failed ? Color.red : Color.orange)
            .fixedSize(horizontal: false, vertical: true)
            .accessibilityElement(children: .contain)
            .accessibilityLabel(value.accessibilityLabel)
            .accessibilityIdentifier("content-analysis-propagation-warning")
        }
    }

    private var currentPropagationOutcome: ContentAnalysisMutationOutcome? {
        guard let status = store.contentAnalysisStatus else { return nil }
        return status.enabled ? store.contentConsentOutcome : store.contentRevocationOutcome
    }

    private func syncSelectionFromStatus() {
        guard let status = store.contentAnalysisStatus else { return }
        selectionState.synchronize(
            enabled: status.enabled,
            backend: status.backend,
            disclosureAccepted: status.externalDisclosureAccepted
        )
    }

    private var modeBinding: Binding<ContentAnalysisMode> {
        Binding(
            get: { selectionState.displayedMode },
            set: { mode in
                if selectionState.select(mode) {
                    showRevokeConfirmation = true
                }
            }
        )
    }
}
