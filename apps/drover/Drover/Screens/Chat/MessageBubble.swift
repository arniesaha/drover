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
            ToolCard(symbolName: "wrench.fill", title: toolName, detail: toolDetail,
                     editDiff: EditDiff(message: message))
        case .approvalPrompt:
            ToolCard(symbolName: "hand.raised.fill",
                     title: "Approval requested: \(toolName)", detail: toolDetail,
                     editDiff: EditDiff(message: message))
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

    // Thinking messages never reach this view — ChatView groups consecutive
    // ones into a `TranscriptItem.thinkingRun` rendered by `ThinkingBlock`.
    private var assistantBubble: some View {
        HStack {
            // displayBlocks is segmented once at decode (see HarnessMessage) —
            // this loop only lays out prebuilt values.
            VStack(alignment: .leading, spacing: 8) {
                ForEach(Array(message.displayBlocks.enumerated()), id: \.offset) { _, block in
                    switch block {
                    case .text(let attributed):
                        Text(attributed)
                    case .code(let language, let code):
                        CodeBlockView(language: language, code: code)
                    case .diff(let lines):
                        DiffBlockView(lines: lines)
                    }
                }
                usageFooter(alignment: .leading)
            }
            .padding(10)
            .background(.secondary.opacity(0.12), in: RoundedRectangle(cornerRadius: 12))
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
            VStack(spacing: 4) {
                Text(message.text)
                usageFooter(alignment: .center)
            }
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

    @ViewBuilder
    private func usageFooter(alignment: HorizontalAlignment) -> some View {
        if let summary = TokenUsageSummary(message: message) {
            VStack(alignment: alignment, spacing: 2) {
                Label(summary.compactText, systemImage: "number")
                if let contextText = summary.contextText {
                    Label(contextText, systemImage: "rectangle.expand.vertical")
                }
            }
            .font(.caption2)
            .foregroundStyle(.secondary)
        }
    }
}

/// Compact card shared by tool calls and approval prompts: an SF Symbol, a
/// title, and detail behind a disclosure so long tool input doesn't dominate
/// the transcript. claude-code Edit/MultiEdit payloads show a real diff
/// (`EditDiff` extraction succeeded); everything else keeps the raw detail.
private struct ToolCard: View {
    let symbolName: String
    let title: String
    let detail: String?
    var editDiff: EditDiff? = nil

    var body: some View {
        HStack {
            VStack(alignment: .leading, spacing: 4) {
                Label(title, systemImage: symbolName)
                    .font(.callout)
                if let editDiff {
                    DisclosureGroup("Details") {
                        VStack(alignment: .leading, spacing: 4) {
                            if let filePath = editDiff.filePath {
                                Text(filePath)
                                    .font(.system(.caption2, design: .monospaced))
                                    .foregroundStyle(.secondary)
                            }
                            DiffBlockView(lines: editDiff.diffLines)
                                .accessibilityIdentifier("tool-diff")
                        }
                    }
                    .font(.caption)
                } else if let detail, !detail.isEmpty {
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
