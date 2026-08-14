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
    let harness: String
    let isSending: Bool
    let onSend: () -> Void

    @State private var pickerItems: [PhotosPickerItem] = []

    /// Combined attachment budget: relay hosts cap websocket frames at
    /// 8 MiB, and base64 inflates ~1.37x — 6 MB of JPEG stays safely under.
    private static let maxCombinedBytes = 6 * 1024 * 1024

    var body: some View {
        GlassPromptSurface(
            text: $text,
            attachments: $attachments,
            runPreferences: runPreferences,
            arePreferencesEditable: HarnessRunPreferences.canChangeInExistingSession(harness),
            placeholder: "Add feedback...",
            isSending: isSending,
            canSend: !isEmpty,
            attachmentAccessibilityIdentifier: "composer-attachment"
        ) {
            PhotosPicker(selection: $pickerItems, maxSelectionCount: 4,
                         matching: .images) {
                Image(systemName: "plus")
                    .font(.system(size: 24, weight: .regular))
                    .foregroundStyle(.primary)
                    .frame(width: 32, height: 32)
                    .contentShape(Circle())
            }
            .accessibilityLabel("Attach image")
            .accessibilityIdentifier("composer-attach")
        } onSend: {
            onSend()
        }
        .padding(.horizontal, 16)
        .padding(.top, 8)
        .padding(.bottom, 10)
        .onChange(of: pickerItems) { _, items in
            guard !items.isEmpty else { return }
            pickerItems = []
            Task { await load(items) }
        }
    }

    private func load(_ items: [PhotosPickerItem]) async {
        for item in items {
            guard let raw = try? await item.loadTransferable(type: Data.self),
                  let jpeg = ImageDownscaler.jpegData(from: raw) else { continue }
            let combined = attachments.reduce(0) { $0 + $1.data.count }
            guard combined + jpeg.count <= Self.maxCombinedBytes else { continue }
            attachments.append(TurnAttachment(mediaType: "image/jpeg", data: jpeg))
        }
    }

    private var isEmpty: Bool {
        text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && attachments.isEmpty
    }
}
