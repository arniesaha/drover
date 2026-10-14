import SwiftUI
import PhotosUI
import DroverKit

/// Bottom chat input for structured sessions. The prompt surface owns the
/// glass dock treatment; this wrapper keeps PhotosPicker loading and send
/// wiring local to chat.
struct Composer: View {
    @Binding var text: String
    @Binding var attachments: [TurnAttachment]
    let runPreferences: HarnessModelCatalogState
    /// What the session's host advertises for its harness; drives the
    /// preference chips, their lock and the attach button.
    let controls: HarnessControls
    let isSending: Bool
    let canSend: Bool
    let canAddAttachments: Bool
    let onAddAttachment: (TurnAttachment) async -> Bool
    let onSend: () -> Void

    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @State private var pickerItems: [PhotosPickerItem] = []

    /// Combined attachment budget: relay hosts cap websocket frames at
    /// 8 MiB, and base64 inflates ~1.37x — 6 MB of JPEG stays safely under.
    private static let maxCombinedBytes = 6 * 1024 * 1024

    var body: some View {
        // Bound the scrollable input, not the fixed action row. XXXL chips
        // can reflow inside it without consuming the transcript viewport.
        promptSurface
            .frame(maxHeight: dynamicTypeSize.isAccessibilitySize ? 200 : nil)
            .floatingChatSurface(in: RoundedRectangle(cornerRadius: 30, style: .continuous))
            .padding(.horizontal, 16)
            .padding(.top, 8)
            .padding(.bottom, 10)
            .onChange(of: pickerItems) { _, items in
                guard !items.isEmpty else { return }
                pickerItems = []
                Task { await load(items) }
            }
    }

    private var promptSurface: some View {
        GlassPromptSurface(
            text: $text,
            attachments: $attachments,
            runPreferences: runPreferences,
            showsPreferences: controls.showsModelControls,
            arePreferencesEditable: HarnessRunPreferences.canChangeInExistingSession(controls),
            preferencesLockedReason: controls.preferencesLockedReason,
            placeholder: "Add feedback...",
            isSending: isSending,
            canSend: !isEmpty && canSend,
            scrollsInput: dynamicTypeSize.isAccessibilitySize,
            floatsInChat: true,
            attachmentAccessibilityIdentifier: "composer-attachment"
        ) {
            PhotosPicker(selection: $pickerItems, maxSelectionCount: 4,
                         matching: .images) {
                Image(systemName: "plus")
                    .font(.system(size: 24, weight: .regular))
                    .foregroundStyle(.primary)
                    .frame(width: 44, height: 44)
                    .contentShape(Circle())
            }
            .accessibilityLabel("Attach image")
            .accessibilityHint(controls.attachmentsUnavailableReason ?? "")
            .accessibilityIdentifier("composer-attach")
            .disabled(!canAddAttachments || !controls.acceptsImageAttachments)
            .opacity(canAddAttachments && controls.acceptsImageAttachments ? 1 : 0.45)
        } onSend: {
            onSend()
        }
    }

    private func load(_ items: [PhotosPickerItem]) async {
        for item in items {
            guard let raw = try? await item.loadTransferable(type: Data.self),
                  let jpeg = ImageDownscaler.jpegData(from: raw) else { continue }
            let combined = attachments.reduce(0) { $0 + $1.data.count }
            guard attachments.count < 4,
                  combined + jpeg.count <= Self.maxCombinedBytes
            else { continue }
            guard await onAddAttachment(
                TurnAttachment(mediaType: "image/jpeg", data: jpeg)
            ) else { return }
        }
    }

    private var isEmpty: Bool {
        text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && attachments.isEmpty
    }
}
