import SwiftUI
import DroverKit

struct HarnessPreferenceControls: View {
    let runPreferences: HarnessModelCatalogState
    let isEditable: Bool
    /// Spoken by VoiceOver on the lock: why these can't change right now.
    var lockedReason: String? = nil

    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @State private var showsModelPicker = false

    var body: some View {
        @Bindable var runPreferences = runPreferences
        let effort = HarnessModelPickerPresentation.effort(
            in: runPreferences.catalog,
            selectedModel: runPreferences.selectedModel,
            selectedEffort: runPreferences.thinkingEffort
        )

        // Normal text keeps both choices on one row. Accessibility sizes
        // may wrap inside the scrollable input, leaving Send in its own row.
        let layout = dynamicTypeSize.isAccessibilitySize
            ? AnyLayout(FlowLayout(spacing: 8))
            : AnyLayout(HStackLayout(spacing: 8))
        layout {
            Button {
                showsModelPicker = true
            } label: {
                PreferenceChip(
                    title: HarnessModelCatalogPresentation.modelTitle(
                        selection: runPreferences.selectedModel,
                        catalog: runPreferences.catalog
                    ),
                    systemImage: "cpu",
                    kind: "Model",
                    wraps: dynamicTypeSize.isAccessibilitySize
                )
            }
            .buttonStyle(.plain)
            .accessibilityIdentifier("composer-model")

            if let effort {
                Menu {
                    ForEach(effort.choices) { choice in
                        Button(choice.title) {
                            runPreferences.thinkingEffort = choice.rawValue
                        }
                    }
                } label: {
                    PreferenceChip(
                        title: effort.title,
                        systemImage: "brain",
                        kind: "Thinking effort",
                        wraps: dynamicTypeSize.isAccessibilitySize
                    )
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("composer-effort")
            }

            if !isEditable {
                Image(systemName: "lock.fill")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                    .accessibilityLabel("Run preferences locked for this session")
                    .accessibilityHint(lockedReason ?? "")
                    .accessibilityIdentifier("run-preferences-locked")
            }
        }
        .layoutPriority(1)
        .disabled(!isEditable)
        .opacity(isEditable ? 1 : 0.7)
        .sheet(isPresented: $showsModelPicker) {
            NavigationStack {
                HarnessModelPicker(
                    runPreferences: runPreferences,
                    isEditable: isEditable
                )
            }
        }
    }
}

private struct PreferenceChip: View {
    let title: String
    let systemImage: String
    let kind: String
    let wraps: Bool

    var body: some View {
        HStack(spacing: 6) {
            Image(systemName: systemImage)
                .imageScale(.medium)
            Text(title)
                .lineLimit(wraps ? nil : 1)
                .truncationMode(.tail)
                .fixedSize(horizontal: false, vertical: true)
        }
        .font(.callout.weight(.medium))
        .padding(.horizontal, 10)
        .padding(.vertical, 7)
        .background(.ultraThinMaterial, in: Capsule())
        .overlay(Capsule().strokeBorder(.secondary.opacity(0.18)))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(kind): \(title)")
    }
}
