import SwiftUI
import PhotosUI
import NexusKit

/// The entry row pinned to the bottom of `ChatView`: a paperclip photo
/// picker, a growing text field, and a send button — plus a removable
/// thumbnail strip when images are attached. No chat logic — `onSend` is
/// wired by the caller to `ChatModel.sendTurn()`, and picked images land in
/// the bound `attachments` (already downscaled to JPEG).
struct Composer: View {
    @Binding var text: String
    @Binding var attachments: [TurnAttachment]
    let onSend: () -> Void

    @State private var pickerItems: [PhotosPickerItem] = []

    /// Combined attachment budget: relay hosts cap websocket frames at
    /// 8 MiB, and base64 inflates ~1.37x — 6 MB of JPEG stays safely under.
    private static let maxCombinedBytes = 6 * 1024 * 1024

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if !attachments.isEmpty {
                attachmentStrip
            }
            HStack(alignment: .bottom, spacing: 8) {
                PhotosPicker(selection: $pickerItems, maxSelectionCount: 4,
                             matching: .images) {
                    Image(systemName: "paperclip")
                        .font(.title3)
                }
                .accessibilityLabel("Attach image")
                .accessibilityIdentifier("composer-attach")

                TextField("Message", text: $text, axis: .vertical)
                    .textFieldStyle(.roundedBorder)
                    .lineLimit(1...5)

                Button(action: onSend) {
                    Image(systemName: "arrow.up.circle.fill")
                        .font(.title)
                }
                .disabled(isEmpty)
                .accessibilityLabel("Send")
                .accessibilityIdentifier("composer-send")
            }
        }
        .padding(.horizontal)
        .padding(.vertical, 8)
        .onChange(of: pickerItems) { _, items in
            guard !items.isEmpty else { return }
            pickerItems = []
            Task { await load(items) }
        }
    }

    private var attachmentStrip: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(Array(attachments.enumerated()), id: \.offset) { index, attachment in
                    thumbnail(for: attachment)
                        .overlay(alignment: .topTrailing) {
                            Button {
                                attachments.remove(at: index)
                            } label: {
                                Image(systemName: "xmark.circle.fill")
                                    .font(.caption)
                                    .foregroundStyle(.white, .black.opacity(0.6))
                            }
                            .accessibilityLabel("Remove attachment")
                        }
                        .accessibilityIdentifier("composer-attachment")
                }
            }
        }
    }

    @ViewBuilder
    private func thumbnail(for attachment: TurnAttachment) -> some View {
        if let image = UIImage(data: attachment.data) {
            Image(uiImage: image)
                .resizable()
                .scaledToFill()
                .frame(width: 44, height: 44)
                .clipShape(RoundedRectangle(cornerRadius: 8))
        } else {
            Image(systemName: "photo")
                .frame(width: 44, height: 44)
                .background(.secondary.opacity(0.2),
                            in: RoundedRectangle(cornerRadius: 8))
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
