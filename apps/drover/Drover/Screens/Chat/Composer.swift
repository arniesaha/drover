import SwiftUI

/// The text-entry row pinned to the bottom of `ChatView`: a growing text
/// field plus a send button, disabled while empty. No chat logic — `onSend`
/// is wired by the caller to `ChatModel.sendTurn()`.
struct Composer: View {
    @Binding var text: String
    let onSend: () -> Void

    var body: some View {
        HStack(alignment: .bottom, spacing: 8) {
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
        .padding(.horizontal)
        .padding(.vertical, 8)
    }

    private var isEmpty: Bool {
        text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }
}
