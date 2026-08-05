import SwiftUI
import NexusKit

/// One collapsed row per tool step: a `tool_action` paired (or awaiting
/// pairing) with its `tool_result`. Collapsed shows tool name + one-line
/// status (`running…` / ✓ / ✗); expanded shows the input and full result
/// through the shared code/diff rendering. This is the "everything
/// intermediate is compact" half of the M5 transcript design — final
/// assistant output stays full-size in MessageBubble.
struct StepCard: View {
    let action: HarnessMessage
    let result: HarnessMessage?
    @State private var isExpanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Button {
                withAnimation(.snappy(duration: 0.2)) { isExpanded.toggle() }
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: "wrench.fill")
                    Text(title).lineLimit(1)
                    Spacer(minLength: 8)
                    statusChip
                    Image(systemName: "chevron.right")
                        .font(.caption2.weight(.semibold))
                        .rotationEffect(.degrees(isExpanded ? 90 : 0))
                }
                .font(.callout)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityIdentifier("step-card")

            if isExpanded {
                VStack(alignment: .leading, spacing: 6) {
                    if let editDiff = EditDiff(message: action) {
                        if let filePath = editDiff.filePath {
                            Text(filePath)
                                .font(.system(.caption2, design: .monospaced))
                                .foregroundStyle(.secondary)
                        }
                        DiffBlockView(lines: editDiff.diffLines)
                    } else if let command = action.payload["input"]?.objectValue?["command"]?.stringValue {
                        CodeBlockView(language: "sh", code: command)
                    } else if let input = action.payload["input"]?.displayString, !input.isEmpty {
                        Text(input)
                            .font(.system(.caption, design: .monospaced))
                            .foregroundStyle(.secondary)
                    }
                    if let result, !result.text.isEmpty {
                        Divider()
                        ForEach(Array(result.displayBlocks.enumerated()), id: \.offset) { _, block in
                            switch block {
                            case .text(let attributed):
                                Text(attributed).font(.caption)
                            case .code(let language, let code):
                                CodeBlockView(language: language, code: code)
                            case .diff(let lines):
                                DiffBlockView(lines: lines)
                            case .heading(_, let content):
                                // Tool results are compact captions; a heading
                                // inside one keeps caption size, just bolder.
                                Text(content).font(.caption.bold())
                            }
                        }
                    }
                }
                .transition(.opacity)
            }
        }
        .padding(10)
        .background(.blue.opacity(0.10), in: RoundedRectangle(cornerRadius: 10))
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var title: String {
        StepCardPresentation.title(for: action)
    }

    @ViewBuilder
    private var statusChip: some View {
        if let result {
            let status = StepCardPresentation.status(for: result)
            Label(
                status.label,
                systemImage: status.failed ? "xmark.circle" : "checkmark.circle"
            )
                .font(.caption)
                .foregroundStyle(status.failed ? .red : .secondary)
        } else {
            HStack(spacing: 4) {
                ProgressView().controlSize(.mini)
                Text("running…")
            }
            .font(.caption)
            .foregroundStyle(.secondary)
        }
    }
}

struct StepCardPresentation {
    static func title(for action: HarnessMessage) -> String {
        let tool = action.payload["tool"]?.stringValue ?? action.text
        if let command = action.payload["input"]?.objectValue?["command"]?.stringValue,
           let firstLine = command.split(separator: "\n").first {
            return String(firstLine.prefix(72))
        }
        if let filePath = action.payload["input"]?.objectValue?["file_path"]?.stringValue {
            return "\(tool) \(URL(fileURLWithPath: filePath).lastPathComponent)"
        }
        return tool
    }

    static func status(for result: HarnessMessage) -> (failed: Bool, label: String) {
        let exitCode = result.payload["exit_code"]?.numberValue.map { Int($0) }
        let resultStatus = result.payload["status"]?.stringValue
        let failed = (exitCode ?? 0) != 0
            || ["failed", "error"].contains(resultStatus)
            || result.payload["is_error"]?.boolValue == true
        let label = failed ? (exitCode.map { "exit \($0)" } ?? "failed") : "done"
        return (failed, label)
    }
}
