import SwiftUI
import DroverKit

/// A run of consecutive tool calls as one fold: `6 steps · 42s · all clean`
/// collapsed, a mono list of commands expanded.
///
/// Each line inside the run stays individually expandable, so the diff or the
/// command output is still one tap away — the fold compresses the *summary*,
/// it does not throw the detail away.
struct StepRunCard: View {
    let steps: [ToolStep]

    var body: some View {
        FoldRow(
            systemImage: "terminal",
            summary: FoldSummary.steps(steps),
            accessibilityIdentifier: "step-run",
            isStreaming: steps.contains(where: \.isRunning)
        ) {
            ForEach(steps) { step in
                StepLine(step: step)
            }
        }
    }
}

/// One command inside a run: outcome mark, the command itself, elapsed time.
/// Machine strings never wrap — the command truncates in the middle so both
/// the binary and its most distinguishing argument survive.
private struct StepLine: View {
    let step: ToolStep
    @State private var isExpanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Button {
                withAnimation(.snappy(duration: 0.2)) { isExpanded.toggle() }
            } label: {
                HStack(alignment: .firstTextBaseline, spacing: 7) {
                    Text(mark)
                        .droverText(.marker)
                        .foregroundStyle(step.isFailed ? DroverColor.accent : DroverColor.accentHi)
                        .frame(width: 10, alignment: .leading)

                    Text(StepCardPresentation.title(for: step.action))
                        .droverText(.mono)
                        .foregroundStyle(DroverColor.muted)
                        .lineLimit(1)
                        .truncationMode(.middle)

                    Spacer(minLength: 6)

                    if let duration = step.duration {
                        Text(FoldSummary.duration(duration))
                            .droverText(.mono)
                            .fixedSize()
                    }
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityIdentifier("step-line")

            if isExpanded {
                VStack(alignment: .leading, spacing: 6) {
                    detail
                }
                .transition(.opacity)
            }
        }
    }

    private var mark: String {
        if step.isRunning { return "·" }
        return step.isFailed ? "✗" : "✓"
    }

    @ViewBuilder
    private var detail: some View {
        if let editDiff = EditDiff(message: step.action) {
            if let filePath = editDiff.filePath {
                Text(filePath).droverText(.mono)
            }
            DiffBlockView(lines: editDiff.diffLines)
        } else if let command = step.action.payload["input"]?.objectValue?["command"]?.stringValue {
            CodeBlockView(language: "sh", code: command)
        } else if let input = step.action.payload["input"]?.displayString, !input.isEmpty {
            Text(input).droverText(.mono)
        }

        if let result = step.result, !result.text.isEmpty {
            ForEach(Array(result.displayBlocks.enumerated()), id: \.offset) { _, block in
                switch block {
                case .text(let attributed):
                    Text(attributed).droverText(.nested)
                case .code(let language, let code):
                    CodeBlockView(language: language, code: code)
                case .diff(let lines):
                    DiffBlockView(lines: lines)
                case .heading(_, let content):
                    Text(content).droverText(.h2)
                case .table(let table):
                    TableBlockView(table: table)
                case .list(let list):
                    ListBlockView(list: list)
                case .quote(let quoted):
                    QuoteBlockView(content: quoted)
                case .rule:
                    FadingRule()
                }
            }
        }
    }
}

enum StepCardPresentation {
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

    /// Kept for the label; the failure predicate itself lives on `ToolStep`
    /// so the collapsed run summary and the expanded line can never disagree.
    static func status(for result: HarnessMessage) -> (failed: Bool, label: String) {
        let failed = ToolStep.isFailure(result)
        let exitCode = result.payload["exit_code"]?.numberValue.map { Int($0) }
        let label = failed ? (exitCode.map { "exit \($0)" } ?? "failed") : "done"
        return (failed, label)
    }
}
