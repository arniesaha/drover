import Foundation

/// Old→new diff extracted from a claude-code `Edit`/`MultiEdit` tool payload
/// (`payload.tool` + `payload.input`). No diff algorithm on purpose: an Edit
/// payload already *is* an old→new pair, so `old_string` lines render as
/// removals and `new_string` lines as additions. Any other harness, tool, or
/// payload shape → `nil`, and the tool card keeps its raw-JSON detail.
public struct EditDiff: Sendable, Equatable {
    public struct Hunk: Sendable, Equatable {
        public var removed: [String]
        public var added: [String]

        public init(removed: [String], added: [String]) {
            self.removed = removed
            self.added = added
        }
    }

    public var filePath: String?
    public var hunks: [Hunk]

    public init?(message: HarnessMessage) {
        guard message.type == .toolAction || message.type == .approvalPrompt,
              let tool = message.payload["tool"]?.stringValue,
              let input = message.payload["input"]?.objectValue else { return nil }
        filePath = input["file_path"]?.stringValue
        switch tool {
        case "Edit":
            guard let hunk = Self.hunk(from: input) else { return nil }
            hunks = [hunk]
        case "MultiEdit":
            guard case .array(let edits)? = input["edits"] else { return nil }
            let parsed = edits.compactMap { $0.objectValue.flatMap(Self.hunk(from:)) }
            guard !parsed.isEmpty, parsed.count == edits.count else { return nil }
            hunks = parsed
        default:
            return nil
        }
    }

    private static func hunk(from object: [String: JSONValue]) -> Hunk? {
        guard let old = object["old_string"]?.stringValue,
              let new = object["new_string"]?.stringValue else { return nil }
        return Hunk(
            removed: old.isEmpty ? [] : old.components(separatedBy: "\n"),
            added: new.isEmpty ? [] : new.components(separatedBy: "\n"))
    }

    /// Flat line list for `DiffBlockView`: removals then additions per hunk,
    /// with an `@@ edit N @@` separator between MultiEdit hunks.
    public var diffLines: [DiffLine] {
        var lines: [DiffLine] = []
        for (index, hunk) in hunks.enumerated() {
            if index > 0 {
                lines.append(DiffLine(kind: .hunk, text: "@@ edit \(index + 1) @@"))
            }
            lines.append(contentsOf: hunk.removed.map { DiffLine(kind: .remove, text: "- " + $0) })
            lines.append(contentsOf: hunk.added.map { DiffLine(kind: .add, text: "+ " + $0) })
        }
        return lines
    }
}
