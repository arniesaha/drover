import Foundation

/// Labels for a folded status run. Pure string-building so the row that
/// renders it stays presentational and this stays testable.
public enum SessionEventSummary {
    public static func title(for run: [HarnessMessage]) -> String {
        guard run.count > 1 else {
            return run.first.map(name) ?? "session event"
        }
        return "\(run.count) session events"
    }

    /// Surface the one field that makes each event worth reading; fall back
    /// to the bare kind when a payload has nothing useful.
    public static func detail(for message: HarnessMessage) -> String {
        let kind = name(message)
        if let hook = message.payload["hook_name"]?.stringValue {
            let outcome = message.payload["outcome"]?.stringValue ?? "ran"
            return "\(hook) — \(outcome)"
        }
        if let description = message.payload["description"]?.stringValue {
            return "\(kind) — \(description)"
        }
        if let summary = message.payload["summary"]?.stringValue {
            let state = message.payload["status"]?.stringValue ?? ""
            return state.isEmpty ? "\(kind) — \(summary)" : "\(kind) — \(summary) (\(state))"
        }
        if let elapsed = message.payload["elapsed_time_seconds"]?.numberValue {
            let tool = message.payload["tool_name"]?.stringValue ?? "tool"
            return "\(tool) running — \(Int(elapsed))s"
        }
        return kind
    }

    private static func name(_ message: HarnessMessage) -> String {
        message.text.isEmpty ? "session event" : message.text
    }
}
