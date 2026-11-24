import SwiftUI
import DroverKit

struct HarnessPreferenceControls: View {
    let runPreferences: HarnessModelCatalogState
    let isEditable: Bool
    /// Spoken by VoiceOver on the lock: why these can't change right now.
    var lockedReason: String? = nil
    /// Chat uses compact, symbolic controls at accessibility sizes so its fixed
    /// action row and complete preference targets share the available dock.
    var usesCompactAccessibilityPresentation = false

    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @State private var showsModelPicker = false
    @State private var showsEffortPicker = false

    var body: some View {
        @Bindable var runPreferences = runPreferences
        let effort = HarnessModelPickerPresentation.effort(
            in: runPreferences.catalog,
            selectedModel: runPreferences.selectedModel,
            selectedEffort: runPreferences.thinkingEffort
        )

        // Keep the controls on one complete row at every Dynamic Type size.
        // The composer deliberately caps its input viewport; allowing this
        // pair to flow into a second line left the lower chip behind the
        // Attach/Send row at XXXL. Chat's fitted presentation keeps compact
        // symbolic controls in that one row while their accessibility labels retain
        // the selected values, without making the dock taller or concealing
        // part of a hit target.
        let layout = AnyLayout(HStackLayout(spacing: 8))
        let fittedAccessibilityRow = dynamicTypeSize.isAccessibilitySize
        let modelTitle = HarnessModelCatalogPresentation.modelTitle(
            selection: runPreferences.selectedModel,
            catalog: runPreferences.catalog
        )
        layout {
            Button {
                showsModelPicker = true
            } label: {
                PreferenceChip(
                    title: modelTitle,
                    accessibilityTitle: modelTitle,
                    systemImage: "cpu",
                    kind: "Model",
                    wraps: false,
                    compact: usesCompactAccessibilityPresentation
                )
            }
            .buttonStyle(.plain)
            .accessibilityIdentifier("composer-model")
            .frame(maxWidth: fittedAccessibilityRow ? .infinity : nil, alignment: .leading)

            if let effort {
                // Use the same explicit control/presentation pattern as the
                // model picker. Besides making the two preferences behave
                // alike for VoiceOver, this avoids a platform-specific
                // `Menu` accessibility state that can leave the label present
                // but no longer hittable immediately after a selection.
                Button {
                    showsEffortPicker = true
                } label: {
                    PreferenceChip(
                        title: effort.title,
                        accessibilityTitle: effort.title,
                        systemImage: "brain",
                        kind: "Thinking effort",
                        wraps: false,
                        compact: usesCompactAccessibilityPresentation
                    )
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("composer-effort")
                .frame(maxWidth: fittedAccessibilityRow ? .infinity : nil, alignment: .leading)
                .confirmationDialog("Thinking effort", isPresented: $showsEffortPicker) {
                    ForEach(effort.choices) { choice in
                        Button(choice.title) {
                            runPreferences.thinkingEffort = choice.rawValue
                        }
                    }
                }
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
    let accessibilityTitle: String
    let systemImage: String
    let kind: String
    let wraps: Bool
    let compact: Bool

    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    var body: some View {
        Group {
            if compact {
                Image(systemName: systemImage)
                    .font(.system(size: 18, weight: .semibold))
                    .frame(width: 44, height: 44)
            } else {
                HStack(spacing: 6) {
                    Image(systemName: systemImage)
                        .imageScale(.medium)
                    Text(title)
                        .lineLimit(wraps ? nil : 1)
                        .truncationMode(.tail)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .font(.callout.weight(.medium))
        .padding(.horizontal, compact ? 0 : 10)
        // Accessibility text still uses its selected Dynamic Type category;
        // the chip's 44-point floor supplies its target size without adding
        // decorative vertical padding that would crowd the chat dock.
        .padding(.vertical, compact ? 0 : (dynamicTypeSize.isAccessibilitySize ? 2 : 7))
        .frame(minWidth: 44, minHeight: 44)
        .background(.ultraThinMaterial, in: Capsule())
        .overlay(Capsule().strokeBorder(.secondary.opacity(0.18)))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(kind): \(accessibilityTitle)")
    }
}
