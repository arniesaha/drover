import SwiftUI
import UIKit
import DroverKit

struct GlassPromptSurface<AttachmentButton: View>: View {
    @Binding var text: String
    @Binding var attachments: [TurnAttachment]
    @Binding var selectedModel: String
    @Binding var thinkingEffort: String

    let harness: String
    let placeholder: String
    let sendSystemImage: String
    let isSending: Bool
    let canSend: Bool
    let showsSendButton: Bool
    let attachmentAccessibilityIdentifier: String
    let attachmentButton: AttachmentButton
    let onSend: () -> Void

    @FocusState private var isTextFocused: Bool

    init(
        text: Binding<String>,
        attachments: Binding<[TurnAttachment]>,
        selectedModel: Binding<String>,
        thinkingEffort: Binding<String>,
        harness: String,
        placeholder: String,
        sendSystemImage: String = "arrow.up",
        isSending: Bool = false,
        canSend: Bool = true,
        showsSendButton: Bool = true,
        attachmentAccessibilityIdentifier: String,
        @ViewBuilder attachmentButton: () -> AttachmentButton,
        onSend: @escaping () -> Void = {}
    ) {
        _text = text
        _attachments = attachments
        _selectedModel = selectedModel
        _thinkingEffort = thinkingEffort
        self.harness = harness
        self.placeholder = placeholder
        self.sendSystemImage = sendSystemImage
        self.isSending = isSending
        self.canSend = canSend
        self.showsSendButton = showsSendButton
        self.attachmentAccessibilityIdentifier = attachmentAccessibilityIdentifier
        self.attachmentButton = attachmentButton()
        self.onSend = onSend
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if !attachments.isEmpty {
                attachmentStrip
            }

            TextField(placeholder, text: $text, axis: .vertical)
                .font(.system(size: 22, weight: .regular, design: .default))
                .lineLimit(1...5)
                .textFieldStyle(.plain)
                .focused($isTextFocused)
                .submitLabel(.send)
                .onSubmit {
                    guard showsSendButton, canSend, !isSending else { return }
                    onSend()
                }
                .toolbar { keyboardBar }

            HStack(spacing: 12) {
                attachmentButton
                    .frame(width: 32, height: 32)

                HarnessPreferenceControls(
                    harness: harness,
                    selectedModel: $selectedModel,
                    thinkingEffort: $thinkingEffort
                )

                Spacer(minLength: 8)

                if showsSendButton {
                    Button(action: onSend) {
                        if isSending {
                            ProgressView()
                                .frame(width: 38, height: 38)
                        } else {
                            Image(systemName: sendSystemImage)
                                .font(.system(size: 18, weight: .bold))
                                .foregroundStyle(.white)
                                .frame(width: 38, height: 38)
                                .background(sendFill, in: Circle())
                        }
                    }
                    .buttonStyle(.plain)
                    .disabled(!canSend || isSending)
                    .opacity(canSend || isSending ? 1 : 0.45)
                    .accessibilityLabel(isSending ? "Sending" : "Send")
                    .accessibilityIdentifier("composer-send")
                }
            }
        }
        .padding(.horizontal, 18)
        .padding(.top, attachments.isEmpty ? 16 : 14)
        .padding(.bottom, 14)
        // Nocturne separates surfaces by ramp step and a hairline, not by
        // material and drop shadow: the composer is `sheet` lifted off `bg`,
        // outlined in `line`. Dropping the shadow also drops this view's last
        // reason to know which theme it is in.
        .background(DroverColor.sheet, in: RoundedRectangle(cornerRadius: 30, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 30, style: .continuous)
                .strokeBorder(DroverColor.line, lineWidth: 1)
        }
    }

    /// The one strip of chrome iOS guarantees sits above the keyboard,
    /// whatever the keyboard is.
    ///
    /// Keyboard avoidance moves the composer by the height the keyboard
    /// reports, and a third-party keyboard with its own accessory row (Wispr
    /// Flow, and most dictation keyboards) reports less than it draws — the
    /// composer lands *under* it, send and all. Rather than chase that
    /// measurement, send gets a second home in the input accessory view, and
    /// there is finally an explicit way to put the keyboard away.
    @ToolbarContentBuilder
    private var keyboardBar: some ToolbarContent {
        ToolbarItemGroup(placement: .keyboard) {
            Button {
                isTextFocused = false
            } label: {
                Label("Hide keyboard", systemImage: "keyboard.chevron.compact.down")
            }
            .accessibilityIdentifier("keyboard-dismiss")

            Spacer()

            if showsSendButton {
                Button {
                    onSend()
                } label: {
                    Label("Send", systemImage: sendSystemImage)
                        .labelStyle(.titleAndIcon)
                        .fontWeight(.medium)
                }
                .disabled(!canSend || isSending)
                .accessibilityIdentifier("keyboard-send")
            }
        }
    }

    private var sendFill: some ShapeStyle {
        if canSend {
            return AnyShapeStyle(.tint)
        }
        return AnyShapeStyle(.secondary.opacity(0.28))
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
                                    .foregroundStyle(.white, .black.opacity(0.65))
                            }
                            .buttonStyle(.plain)
                            .accessibilityLabel("Remove attachment")
                        }
                        .accessibilityIdentifier(attachmentAccessibilityIdentifier)
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
                .frame(width: 48, height: 48)
                .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
        } else {
            Image(systemName: "photo")
                .frame(width: 48, height: 48)
                .background(.secondary.opacity(0.18),
                            in: RoundedRectangle(cornerRadius: 10, style: .continuous))
        }
    }
}
