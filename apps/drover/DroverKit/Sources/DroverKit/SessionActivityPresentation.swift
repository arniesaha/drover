import Foundation

/// Event-backed activity, cached by ChatModel. A missing result alone cannot
/// keep a tool alive across a completed turn or a new user input.
public struct SessionActivityPresentation: Sendable, Equatable {
    public enum Phase: Sendable { case ready, preparing, working, tool, completed, failed, approval, connecting, reconnecting, sending, delivery, unknown }
    public var phase: Phase = .ready
    public var title = "Ready"
    public var detail: String?
    public var startedAt: Date?
    public var updatedAt: Date?
    public var completedSteps = 0
    public var activeToolIDs: Set<String> = []
    public var isActive: Bool { [.preparing, .working, .tool].contains(phase) }

    public init(messages: [HarnessMessage]) {
        var actions: [String: HarnessMessage] = [:]
        var completed: Set<String> = []
        for message in messages {
            if message.type == .userInput || (message.type == .status && message.text == "turn.started") {
                actions = [:]
                completed = []
                completedSteps = 0
                activeToolIDs = []
                startedAt = message.timestamp
                phase = .preparing
                title = "Preparing"
                detail = nil
            }
            if let timestamp = message.timestamp { updatedAt = timestamp }
            switch message.type {
            case .toolAction:
                if !isActive {
                    actions = [:]
                    completed = []
                    completedSteps = 0
                    activeToolIDs = []
                    startedAt = message.timestamp
                }
                let id = message.payload["tool_use_id"]?.stringValue ?? message.id
                actions[id] = message
                activeToolIDs.insert(id)
                startedAt = startedAt ?? message.timestamp
                phase = .tool
            case .toolResult:
                if let id = message.payload["tool_use_id"]?.stringValue, actions[id] != nil {
                    activeToolIDs.remove(id)
                    completed.insert(id)
                    completedSteps = completed.count
                    if isActive { phase = activeToolIDs.isEmpty ? .working : .tool }
                }
            case .assistantOutput:
                if isActive {
                    phase = activeToolIDs.isEmpty ? .working : .tool
                }
            case .status:
                if message.payload["turn_complete"]?.boolValue == true
                    || message.payload["exited"] != nil
                    || (isActive && message.payload["awaiting"]?.stringValue == "input") {
                    let failed = message.payload["exited"]?.numberValue.map { $0 != 0 } == true
                        || message.payload["result"]?.objectValue?["is_error"]?.boolValue == true
                        || message.payload["error"]?.boolValue == true
                    phase = failed ? .failed : .completed
                    activeToolIDs = []
                    actions = [:]
                }
            case .error, .transcriptGap:
                phase = message.type == .error ? .failed : .unknown
                activeToolIDs = []
                actions = [:]
            default: break
            }
        }
        switch phase {
        case .tool:
            if let action = actions.values.filter({ activeToolIDs.contains($0.payload["tool_use_id"]?.stringValue ?? $0.id) })
                .max(by: { $0.seq < $1.seq }) {
                let tool = (action.payload["tool"]?.stringValue ?? action.text).lowercased()
                let input = action.payload["input"]?.objectValue
                switch tool {
                case "read", "read_file": title = "Reading files"
                case "edit", "write", "apply_patch", "write_file": title = "Editing files"
                case "grep", "glob", "search": title = "Searching files"
                case "bash", "shell", "command_execution": title = "Running command"  // harness-name: provider tool names, not a harness
                default: title = "Using \(action.payload["tool"]?.stringValue ?? "tool")"
                }
                detail = input?["command"]?.stringValue ?? input?["file_path"]?.stringValue
            }
        case .working: title = "Working"; detail = nil
        case .completed: title = "Turn complete"; detail = nil
        case .failed: title = "Run ended with an error"; detail = nil
        case .unknown: title = "Activity unavailable"; detail = nil
        default: break
        }
    }

    func overriding(phase: Phase, title: String) -> Self {
        var value = self
        value.phase = phase
        value.title = title
        value.detail = nil
        value.activeToolIDs = []
        return value
    }
}
