import SwiftUI
import UIKit
import DroverKit

struct GlassPromptSurface<AttachmentButton: View>: View {
    @Binding var text: String
    @Binding var attachments: [TurnAttachment]
    @Binding var selectedModel: String
    @Binding var thinkingEffort: String

    let harness: String
    let arePreferencesEditable: Bool
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
        arePreferencesEditable: Bool = true,
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
        self.arePreferencesEditable = arePreferencesEditable
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

            HStack(alignment: .top, spacing: 8) {
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

                // Only while the keyboard is up — the rest of the time it
                // would be a control for a state you are not in.
                if isTextFocused {
                    dismissKeyboardButton
                }
            }
            .animation(.snappy(duration: 0.2), value: isTextFocused)

            HStack(spacing: 12) {
                attachmentButton
                    .frame(width: 32, height: 32)

                HarnessPreferenceControls(
                    harness: harness,
                    isEditable: arePreferencesEditable,
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

    /// Dismissal lives in the composer, at the trailing end of the text line.
    ///
    /// It began on an input accessory bar, which cost a second strip of
    /// chrome and put a second send arrow directly under the composer's own —
    /// two identical buttons, no way to tell them apart. The obvious next
    /// home was the control row beside the preference chips, and that row has
    /// no room: a 32pt button plus its spacing takes 44 of the ~48pt of slack
    /// there, and the chips collapse to "Def…" at 393pt and "D…"/"A…" at
    /// 375pt. Trading the model you are about to run for a button that hides
    /// a keyboard is the wrong trade.
    ///
    /// The text line, meanwhile, is empty to the right of a short prompt at
    /// every width. Top-aligned, so it stays on the first line as the field
    /// grows to five.
    private var dismissKeyboardButton: some View {
        Button {
            isTextFocused = false
        } label: {
            Image(systemName: "keyboard.chevron.compact.down")
                .font(.system(size: 17, weight: .medium))
                .foregroundStyle(DroverColor.muted)
                .frame(width: 32, height: 30)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .transition(.opacity.combined(with: .scale(scale: 0.85)))
        .accessibilityLabel("Hide keyboard")
        .accessibilityIdentifier("keyboard-dismiss")
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
