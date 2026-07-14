import SwiftUI
import NexusKit

/// Renders one `HarnessMessage` per the chat rendering map: markdown bubbles
/// for assistant/user turns, a compact card for tool calls, a centered
/// caption for status lines, a red-tinted card for errors, and a collapsed
/// monospaced disclosure for anything raw/unrecognized. Purely
/// presentational — no chat logic lives here.
struct MessageBubble: View {
    let message: HarnessMessage

    var body: some View {
        switch message.type {
        case .assistantOutput:
            assistantBubble
        case .userInput:
            userBubble
        case .toolAction, .toolResult:
            ToolCard(symbolName: "wrench.fill", title: toolName, detail: toolDetail)
        case .approvalPrompt:
            ToolCard(symbolName: "hand.raised.fill", title: "Approval requested: \(toolName)", detail: toolDetail)
        case .approvalResponse:
            approvalResponseCaption
        case .status:
            statusCaption
        case .error:
            errorCard
        case .raw, .unknown:
            rawDisclosure
        }
    }

    private var assistantBubble: some View {
        HStack {
            if message.payload["thinking"]?.boolValue == true {
                DisclosureGroup("Thinking…") {
                    Text(message.text)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .font(.callout)
            } else {
                // displayText is markdown parsed once at decode —
                // `Text(.init(...))` would re-parse on every render pass,
                // which is measurable during long streams.
                Text(message.displayText)
                    .padding(10)
                    .background(.secondary.opacity(0.12), in: RoundedRectangle(cornerRadius: 12))
            }
            Spacer(minLength: 32)
        }
    }

    private var userBubble: some View {
        HStack {
            Spacer(minLength: 32)
            Text(message.displayText)
                .padding(10)
                .foregroundStyle(.white)
                .background(.tint, in: RoundedRectangle(cornerRadius: 12))
        }
    }

    private var statusCaption: some View {
        HStack {
            Spacer()
            Text(message.text)
                .font(.caption)
                .foregroundStyle(.secondary)
            Spacer()
        }
    }

    private var approvalResponseCaption: some View {
        let allowed = message.payload["decision"]?.stringValue == "allow"
        return HStack {
            Spacer()
            Label(allowed ? "Approved" : "Denied",
                  systemImage: allowed ? "checkmark.circle" : "xmark.circle")
                .font(.caption)
                .foregroundStyle(.secondary)
            Spacer()
        }
    }

    private var errorCard: some View {
        Label(message.text, systemImage: "exclamationmark.triangle.fill")
            .font(.callout)
            .foregroundStyle(.red)
            .padding(10)
            .background(.red.opacity(0.12), in: RoundedRectangle(cornerRadius: 10))
            .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var rawDisclosure: some View {
        DisclosureGroup("Raw event") {
            Text(message.text)
                .font(.system(.caption, design: .monospaced))
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .font(.caption)
    }

    private var toolName: String {
        message.payload["tool"]?.stringValue ?? message.text
    }

    private var toolDetail: String? {
        message.payload["input"]?.displayString ?? message.payload["result"]?.displayString
    }
}

/// Compact card shared by tool calls and approval prompts: an SF Symbol,
/// a title, and an optional detail behind a disclosure so long tool input
/// doesn't dominate the transcript.
private struct ToolCard: View {
    let symbolName: String
    let title: String
    let detail: String?

    var body: some View {
        HStack {
            VStack(alignment: .leading, spacing: 4) {
                Label(title, systemImage: symbolName)
                    .font(.callout)
                if let detail, !detail.isEmpty {
                    DisclosureGroup("Details") {
                        Text(detail)
                            .font(.system(.caption, design: .monospaced))
                            .foregroundStyle(.secondary)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    .font(.caption)
                }
            }
            .padding(10)
            .background(.blue.opacity(0.10), in: RoundedRectangle(cornerRadius: 10))
            Spacer(minLength: 32)
        }
    }
}
