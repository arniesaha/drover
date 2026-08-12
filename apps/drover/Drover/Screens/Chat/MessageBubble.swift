import SwiftUI
import UIKit
import DroverKit

/// Renders one `HarnessMessage` per the chat rendering map: markdown bubbles
/// for assistant/user turns, a compact card for tool calls, a centered
/// caption for status lines, a red-tinted card for errors, and a collapsed
/// monospaced disclosure for anything raw/unrecognized. Purely
/// presentational — no chat logic lives here.
struct MessageBubble: View {
    let message: HarnessMessage

    /// Read only to colour link runs, which need a concrete `Color` rather
    /// than a `PaletteToken` — see `DroverLinkGround`.
    @Environment(\.colorScheme) private var colorScheme

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
        case .transcriptGap:
            gapCaption
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
            // A single text column at one rhythm, with headings as the only
            // size jump — that is what keeps a heading, a table, a code block
            // and a list reading as one document.
            VStack(alignment: .leading, spacing: 13) {
                ForEach(Array(message.displayBlocks.enumerated()), id: \.offset) { _, block in
                    switch block {
                    case .text(let attributed):
                        Text(linked(attributed)).droverText(.body)
                    case .code(let language, let code):
                        CodeBlockView(language: language, code: code)
                    case .diff(let lines):
                        DiffBlockView(lines: lines)
                    case .heading(let level, let content):
                        Text(linked(content)).droverText(level <= 1 ? .h1 : (level == 2 ? .h2 : .h3))
                    case .table(let table):
                        TableBlockView(table: table)
                    case .list(let list):
                        ListBlockView(list: list)
                    case .quote(let content):
                        QuoteBlockView(content: content)
                    case .rule:
                        FadingRule()
                    }
                }
                usageFooter(alignment: .leading)
            }
            .padding(12)
            .background(DroverColor.surface, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
            .overlay {
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .strokeBorder(DroverColor.line, lineWidth: 1)
            }
            .contextMenu { copyButton }
            Spacer(minLength: 32)
        }
    }

    private func linked(_ content: AttributedString) -> AttributedString {
        content.droverLinks(on: .surface, in: colorScheme)
    }

    private var copyButton: some View {
        Button {
            UIPasteboard.general.string = message.text
        } label: {
            Label("Copy", systemImage: "doc.on.doc")
        }
    }

    private var userBubble: some View {
        HStack {
            Spacer(minLength: 32)
            VStack(alignment: .trailing, spacing: 4) {
                if !message.text.isEmpty || attachmentCount == 0 {
                    // The bubble's ground is the tint, which is also the
                    // colour SwiftUI would draw a link in — a URL you sent
                    // was invisible until this recoloured it.
                    Text(message.displayText.droverLinks(on: .accent, in: colorScheme))
                }
                if attachmentCount > 0 {
                    Label(attachmentCount == 1 ? "1 image" : "\(attachmentCount) images",
                          systemImage: "paperclip")
                        .font(.caption2)
                }
            }
            .padding(10)
            .foregroundStyle(.white)
            .background(.tint, in: RoundedRectangle(cornerRadius: 12))
            .contextMenu { copyButton }
        }
    }

    /// Count of images the server recorded on this turn (`user_input`
    /// payload `attachments`, added by the harness manager).
    private var attachmentCount: Int {
        if case .array(let items)? = message.payload["attachments"] { return items.count }
        return 0
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

    /// A hole in the transcript, stated rather than hidden. Deliberately not
    /// an error card: nothing is wrong with the session, some of its history
    /// just did not survive the trip to the hub.
    private var gapCaption: some View {
        HStack {
            Spacer()
            Label(message.text, systemImage: "ellipsis.rectangle")
                .font(.caption)
                .foregroundStyle(.secondary)
                .accessibilityIdentifier("transcript-gap")
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
        if let tool = message.payload["tool"]?.stringValue { return tool }
        // Old recorded sessions: tool results carried no tool key, and text
        // is the entire output — never use it as a title.
        return message.type == .toolResult ? "Tool result" : message.text
    }

    private var toolDetail: String? {
        if message.type == .toolResult {
            return message.text.isEmpty
                ? message.payload["result"]?.displayString : message.text
        }
        return message.payload["input"]?.displayString
            ?? message.payload["result"]?.displayString
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
