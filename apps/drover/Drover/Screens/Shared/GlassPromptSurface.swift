import SwiftUI
import UIKit
import NexusKit

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

    @Environment(\.colorScheme) private var colorScheme
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
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 30, style: .continuous))
        .background(surfaceTint, in: RoundedRectangle(cornerRadius: 30, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 30, style: .continuous)
                .strokeBorder(borderStyle, lineWidth: 1)
        }
        .shadow(color: .black.opacity(colorScheme == .dark ? 0.4 : 0.16), radius: 20, y: 10)
        .contentShape(RoundedRectangle(cornerRadius: 30, style: .continuous))
        .onTapGesture { isTextFocused = true }
    }

    private var surfaceTint: some ShapeStyle {
        if colorScheme == .dark {
            return AnyShapeStyle(.black.opacity(0.72))
        }
        return AnyShapeStyle(.white.opacity(0.68))
    }

    private var borderStyle: some ShapeStyle {
        if colorScheme == .dark {
            return AnyShapeStyle(.white.opacity(0.12))
        }
        return AnyShapeStyle(.black.opacity(0.08))
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
