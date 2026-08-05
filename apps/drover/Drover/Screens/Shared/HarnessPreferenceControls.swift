import SwiftUI
import NexusKit

struct HarnessPreferenceControls: View {
    let harness: String
    @Binding var selectedModel: String
    @Binding var thinkingEffort: String

    var body: some View {
        HStack(spacing: 8) {
            Menu {
                Button("Default") { selectedModel = "" }
                ForEach(HarnessRunPreferences.modelSuggestions(for: harness), id: \.self) { model in
                    Button(model) { selectedModel = model }
                }
            } label: {
                PreferenceChip(title: modelLabel, systemImage: "cpu")
            }
            .buttonStyle(.plain)

            if HarnessRunPreferences.supportsThinkingEffort(harness) {
                Menu {
                    Button("Default") { thinkingEffort = "" }
                    ForEach(HarnessRunPreferences.thinkingEfforts, id: \.self) { effort in
                        Button(effort.capitalized) { thinkingEffort = effort }
                    }
                } label: {
                    PreferenceChip(title: thinkingLabel, systemImage: "brain")
                }
                .buttonStyle(.plain)
            }
        }
    }

    private var modelLabel: String {
        selectedModel.isEmpty ? "Model" : selectedModel
    }

    private var thinkingLabel: String {
        thinkingEffort.isEmpty ? "Auto" : thinkingEffort.capitalized
    }
}

private struct PreferenceChip: View {
    let title: String
    let systemImage: String

    var body: some View {
        Label(title, systemImage: systemImage)
            .font(.callout.weight(.medium))
            .lineLimit(1)
            .truncationMode(.middle)
            .labelStyle(.titleAndIcon)
            .padding(.horizontal, 10)
            .padding(.vertical, 7)
            .frame(maxWidth: 140)
            .background(.ultraThinMaterial, in: Capsule())
            .overlay(Capsule().strokeBorder(.secondary.opacity(0.18)))
    }
}
