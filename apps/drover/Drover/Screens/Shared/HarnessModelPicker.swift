import Foundation
import SwiftUI
import DroverKit

struct HarnessModelPickerRow: Identifiable, Equatable {
    let selection: String
    let title: String
    let modelID: String?
    let description: String?

    var id: String { selection }

    var accessibilityLabel: String {
        guard let modelID else { return title }
        return "\(title), model ID \(modelID)"
    }
}

struct HarnessModelEffortChoice: Identifiable, Equatable {
    let rawValue: String
    let title: String

    var id: String { rawValue }
}

struct HarnessModelEffortPresentation: Equatable {
    let title: String
    let choices: [HarnessModelEffortChoice]
}

struct HarnessModelPickerStatus: Equatable {
    let freshnessText: String?
    let detailText: String?
    let retryTitle: String
}

enum HarnessModelPickerPresentation {
    static func rows(
        in catalog: HarnessModelCatalog?,
        query: String
    ) -> [HarnessModelPickerRow] {
        let defaultRow = HarnessModelPickerRow(
            selection: "",
            title: "Harness default",
            modelID: nil,
            description: nil
        )
        let nativeRows = HarnessModelCatalogPresentation.filteredModels(
            in: catalog,
            query: query
        ).map { model in
            HarnessModelPickerRow(
                selection: model.id,
                title: model.displayName,
                modelID: model.id,
                description: model.description
            )
        }
        return [defaultRow] + nativeRows
    }

    static func effort(
        in catalog: HarnessModelCatalog?,
        selectedModel: String,
        selectedEffort: String
    ) -> HarnessModelEffortPresentation? {
        guard let reasoning = catalog?.reasoning(for: selectedModel) else { return nil }
        let autoTitle: String
        if let nativeDefault = reasoning.default {
            let defaultTitle = HarnessModelCatalogPresentation.title(
                forRawEffort: nativeDefault
            )
            autoTitle = "Auto (\(defaultTitle))"
        } else {
            autoTitle = "Auto"
        }
        let choices = [HarnessModelEffortChoice(rawValue: "", title: autoTitle)]
            + reasoning.supported.map { effort in
                HarnessModelEffortChoice(
                    rawValue: effort,
                    title: HarnessModelCatalogPresentation.title(forRawEffort: effort)
                )
            }
        let title = selectedEffort.isEmpty
            ? autoTitle
            : HarnessModelCatalogPresentation.title(forRawEffort: selectedEffort)
        return HarnessModelEffortPresentation(title: title, choices: choices)
    }

    static func status(
        catalog: HarnessModelCatalog?,
        statusMessage: String?,
        now: Date
    ) -> HarnessModelPickerStatus {
        let freshnessText: String?
        if let catalog {
            freshnessText = HarnessModelCatalogPresentation.staleText(catalog, now: now)
        } else {
            freshnessText = "Never refreshed"
        }
        let detailText = nonEmpty(statusMessage) ?? safeReason(catalog?.staleReason)
        return HarnessModelPickerStatus(
            freshnessText: freshnessText,
            detailText: detailText,
            retryTitle: "Retry"
        )
    }

    static func shouldRefreshOnPresentation(
        catalog: HarnessModelCatalog?,
        now: Date
    ) -> Bool {
        guard let catalog else { return true }
        guard !catalog.stale, let discoveredAt = catalog.discoveredAt else { return true }
        return now.timeIntervalSince(discoveredAt) >= 5 * 60
    }

    private static func nonEmpty(_ value: String?) -> String? {
        guard let value else { return nil }
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }

    private static func safeReason(_ reason: String?) -> String? {
        switch reason {
        case "offline": "Host is offline."
        case "timeout": "Model discovery timed out."
        case "not_authenticated": "Sign in to refresh models."
        case "unsupported": "This harness does not support model discovery."
        case "protocol_error": "The harness returned an invalid model catalog."
        case "refresh_failed": "Could not refresh models."
        case .some: "Could not refresh models."
        case nil: nil
        }
    }
}

struct HarnessModelPicker: View {
    let runPreferences: HarnessModelCatalogState
    let isEditable: Bool

    @Environment(\.dismiss) private var dismiss
    @State private var query = ""

    var body: some View {
        @Bindable var runPreferences = runPreferences
        let rows = HarnessModelPickerPresentation.rows(
            in: runPreferences.catalog,
            query: query
        )
        let status = HarnessModelPickerPresentation.status(
            catalog: runPreferences.catalog,
            statusMessage: runPreferences.statusMessage,
            now: .now
        )

        List {
            Section {
                ForEach(rows) { row in
                    Button {
                        runPreferences.selectedModel = row.selection
                        dismiss()
                    } label: {
                        modelRow(
                            row,
                            selected: runPreferences.selectedModel == row.selection
                        )
                    }
                    .buttonStyle(.plain)
                    .disabled(!isEditable)
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel(row.accessibilityLabel)
                    .accessibilityValue(
                        runPreferences.selectedModel == row.selection ? "Selected" : ""
                    )
                }
            }

            Section {
                if runPreferences.isRefreshing {
                    HStack(spacing: 10) {
                        ProgressView()
                        Text("Refreshing models…")
                    }
                    .accessibilityElement(children: .combine)
                }

                if let freshnessText = status.freshnessText {
                    Text(freshnessText)
                        .foregroundStyle(.secondary)
                }

                if let detailText = status.detailText {
                    Text(detailText)
                        .foregroundStyle(.secondary)
                }

                Button {
                    Task { await runPreferences.refresh(force: true) }
                } label: {
                    Label(status.retryTitle, systemImage: "arrow.clockwise")
                }
                .disabled(runPreferences.isRefreshing)
                .accessibilityLabel("Retry model discovery")
            }
        }
        .navigationTitle("Model")
        .navigationBarTitleDisplayMode(.inline)
        .searchable(text: $query, prompt: "Search models")
        .toolbar {
            ToolbarItem(placement: .confirmationAction) {
                Button("Done") { dismiss() }
            }
        }
        .task {
            guard !runPreferences.isRefreshing,
                  HarnessModelPickerPresentation.shouldRefreshOnPresentation(
                    catalog: runPreferences.catalog,
                    now: .now
                  ) else { return }
            await runPreferences.refresh()
        }
    }

    private func modelRow(_ row: HarnessModelPickerRow, selected: Bool) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                Text(row.title)
                    .foregroundStyle(.primary)

                if let modelID = row.modelID, modelID != row.title {
                    Text(modelID)
                        .font(.caption.monospaced())
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                }

                if let description = row.description {
                    Text(description)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }

            Spacer(minLength: 8)

            if selected {
                Image(systemName: "checkmark")
                    .font(.body.weight(.semibold))
                    .foregroundStyle(.tint)
            }
        }
        .contentShape(Rectangle())
    }
}
