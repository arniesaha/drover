import Foundation

// MARK: - TerminalEvent

public enum TerminalEvent: Sendable, Equatable {
    case output(String)
    case exited
    case detached
    case other(String)
}

// MARK: - TerminalWire

/// Pure JSON codec for the PTY terminal WebSocket protocol
/// (`NexusClient.terminalRequest`). Shapes below were verified against the
/// deployed reference implementation, not assumed from a sketch:
/// `src/nexus/server/harness/daemon.py`'s `_terminal_loop`/
/// `_handle_terminal_message`, and the web client at
/// `src/nexus/server/web/static/harness_terminal.html`.
///
/// Outgoing (client -> server):
/// - `{"type": "input", "data": "<utf8 text>"}` on every keystroke/paste.
/// - `{"type": "resize", "rows": R, "cols": C}` on terminal size change.
///
/// Incoming (server -> client), all handled here except where noted:
/// - `{"type": "attached", "session_id": "..."}` — the *first* frame sent
///   the instant the daemon accepts the WebSocket upgrade (matches the
///   "attached" first frame seen in the earlier two-hop WS smoke test).
///   Not one of the three cases `TerminalEvent` distinguishes; falls into
///   `.other`.
/// - `{"type": "output", "data": "..."}` — PTY output bytes (UTF-8,
///   replacement-decoded server-side) to feed straight into the terminal.
/// - `{"type": "event", "event": {...}}` — a structured-event echo of the
///   same output/input for the session's audit trail; irrelevant to
///   rendering, falls into `.other`.
/// - `{"type": "exit"}` — sent once when the daemon detects the PTY process
///   has died. Note this is **not** `"exited"`: the brief's sketch used
///   that name, but the daemon's actual wire value (see `_terminal_loop`'s
///   `send_json(sock, {"type": "exit"})`) is `"exit"`. Decoded as
///   `.exited` here since that's the semantic the view cares about.
/// - `{"type": "error", "error": "..."}` / `{"type": "pong"}` — control
///   chatter, not surfaced by this codec; `.other`.
///
/// There is no wire frame the daemon ever sends for a detach — a detach is
/// only ever observed as the WebSocket closing (the daemon's `_handle_
/// terminal_message` replies to `"detach"`/`"close"` with a raw WebSocket
/// close frame, no JSON body). `TerminalEvent.detached` and the `"detached"`
/// type string are kept here defensively (decoded if ever sent, and as a
/// case the bridge can synthesize itself from `URLSessionWebSocketTask`'s
/// close event) rather than removed, so the view has one place to react to
/// "the session is gone" regardless of which of the two ways it learns it.
public enum TerminalWire {
    public static func inputFrame(_ text: String) -> String {
        encode(InputFrame(data: text))
    }

    public static func resizeFrame(rows: Int, cols: Int) -> String {
        encode(ResizeFrame(rows: rows, cols: cols))
    }

    public static func decodeOutput(_ frame: String) -> TerminalEvent? {
        guard let data = frame.data(using: .utf8),
              let incoming = try? JSONDecoder().decode(IncomingFrame.self, from: data) else {
            return .other(frame)
        }
        switch incoming.type {
        case "output":
            return .output(incoming.data ?? "")
        case "exit":
            return .exited
        case "detached":
            return .detached
        default:
            return .other(frame)
        }
    }

    // MARK: - Wire shapes

    private struct InputFrame: Encodable {
        let type = "input"
        let data: String
    }

    private struct ResizeFrame: Encodable {
        let type = "resize"
        let rows: Int
        let cols: Int
    }

    private struct IncomingFrame: Decodable {
        let type: String
        let data: String?
    }

    private static func encode(_ value: some Encodable) -> String {
        guard let data = try? JSONEncoder().encode(value),
              let string = String(data: data, encoding: .utf8) else {
            return "{}"
        }
        return string
    }
}
