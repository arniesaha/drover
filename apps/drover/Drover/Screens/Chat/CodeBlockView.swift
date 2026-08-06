import SwiftUI
import DroverKit

/// Fenced code block: language caption + copy button over horizontally
/// scrolling monospaced text on a dark inset. Deliberately dark in both color
/// schemes (same palette family as the terminal screen) so code reads as
/// "console", not prose. No syntax highlighting by design — see the M4 spec.
struct CodeBlockView: View {
    let language: String?
    let code: String

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                if let language {
                    Text(language)
                        .font(.caption2)
                        .foregroundStyle(CodeBlockChrome.dimText)
                }
                Spacer()
                Button {
                    UIPasteboard.general.string = code
                } label: {
                    Image(systemName: "doc.on.doc")
                        .font(.caption)
                        .foregroundStyle(CodeBlockChrome.dimText)
                }
                .accessibilityLabel("Copy code")
                .accessibilityIdentifier("code-block-copy")
            }
            ScrollView(.horizontal, showsIndicators: false) {
                Text(code)
                    .font(.system(.caption, design: .monospaced))
                    .foregroundStyle(CodeBlockChrome.text)
                    .textSelection(.enabled)
            }
        }
        .padding(8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(CodeBlockChrome.background, in: RoundedRectangle(cornerRadius: 8))
        .accessibilityIdentifier("code-block")
    }
}

/// Fenced ```diff blocks and tool-card Edit diffs: one monospaced row per
/// line, tinted by kind. Shares `CodeBlockChrome` with `CodeBlockView`.
struct DiffBlockView: View {
    let lines: [DiffLine]

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            VStack(alignment: .leading, spacing: 0) {
                ForEach(Array(lines.enumerated()), id: \.offset) { _, line in
                    Text(line.text.isEmpty ? " " : line.text)
                        .font(.system(.caption, design: .monospaced))
                        .foregroundStyle(foreground(for: line.kind))
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(rowBackground(for: line.kind))
                }
            }
        }
        .padding(8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(CodeBlockChrome.background, in: RoundedRectangle(cornerRadius: 8))
        .accessibilityIdentifier("diff-block")
    }

    private func foreground(for kind: DiffLine.Kind) -> Color {
        switch kind {
        case .add: .green
        case .remove: .red
        case .hunk: CodeBlockChrome.dimText
        case .context: CodeBlockChrome.text
        }
    }

    private func rowBackground(for kind: DiffLine.Kind) -> Color {
        switch kind {
        case .add: .green.opacity(0.12)
        case .remove: .red.opacity(0.12)
        case .hunk, .context: .clear
        }
    }
}

/// One palette for both block views — matches the terminal screen's
/// near-black background so all "console" surfaces in the app agree.
enum CodeBlockChrome {
    static let background = Color(red: 0.02, green: 0.03, blue: 0.05)
    static let text = Color(red: 0.86, green: 0.91, blue: 0.95)
    static let dimText = Color(red: 0.86, green: 0.91, blue: 0.95).opacity(0.55)
}
