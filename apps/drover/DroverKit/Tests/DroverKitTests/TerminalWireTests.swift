import Foundation
import Testing
@testable import DroverKit

/// Wire shapes verified against the reference implementation (the deployed
/// daemon and web client are the source of truth, not this brief's sketch):
///
/// - Outgoing `input`: `{"type":"input","data":"<text>"}`
/// - Outgoing `resize`: `{"type":"resize","rows":R,"cols":C}`
/// - Incoming `output`: `{"type":"output","data":"..."}`
/// - Incoming process-exit: `{"type":"exit"}` — note the daemon's actual
///   wire value is `"exit"`, not `"exited"` (`_terminal_loop` in
///   `src/nexus/server/harness/daemon.py`).
/// - `"detached"` is never actually sent by the daemon (a detach is only
///   ever observed as the socket closing), but the codec still decodes it
///   defensively if a frame with that type ever arrives.
/// - Anything else recognized-but-irrelevant (`attached`, `event`, `error`,
///   `pong`) or unparseable falls into `.other(frame)`.
struct TerminalWireTests {
    @Test func inputFrameEncodesTypeAndData() throws {
        let frame = TerminalWire.inputFrame("echo drover")
        let decoded = try #require(decodeJSON(frame))
        #expect(decoded["type"] as? String == "input")
        #expect(decoded["data"] as? String == "echo drover")
    }

    @Test func inputFrameEscapesSpecialCharacters() throws {
        // Control chars, quotes, and a newline must round-trip through the
        // JSON encoder correctly, not be interpolated raw.
        let frame = TerminalWire.inputFrame("say \"hi\"\n\u{03}")
        let decoded = try #require(decodeJSON(frame))
        #expect(decoded["data"] as? String == "say \"hi\"\n\u{03}")
    }

    @Test func resizeFrameEncodesRowsAndCols() throws {
        let frame = TerminalWire.resizeFrame(rows: 32, cols: 100)
        let decoded = try #require(decodeJSON(frame))
        #expect(decoded["type"] as? String == "resize")
        #expect(decoded["rows"] as? Int == 32)
        #expect(decoded["cols"] as? Int == 100)
    }

    @Test func interruptFrameEncodesType() throws {
        // The daemon's `interrupt` frame writes Ctrl-C into the PTY —
        // the web client's Ctrl-C button uses it; iOS needs parity.
        let frame = TerminalWire.interruptFrame()
        let decoded = try #require(decodeJSON(frame))
        #expect(decoded["type"] as? String == "interrupt")
        #expect(decoded.count == 1)
    }

    @Test func decodesOutputFrame() {
        let event = TerminalWire.decodeOutput(#"{"type": "output", "data": "hello\r\n"}"#)
        #expect(event == .output("hello\r\n"))
    }

    @Test func decodesOutputFrameWithMissingData() {
        // Defensive: an output frame with no "data" key still decodes, as
        // empty output rather than falling through to .other.
        let event = TerminalWire.decodeOutput(#"{"type": "output"}"#)
        #expect(event == .output(""))
    }

    @Test func decodesExitFrameAsExited() {
        // The daemon's real wire value on process exit is "exit", not
        // "exited" — confirmed in `_terminal_loop`.
        let event = TerminalWire.decodeOutput(#"{"type": "exit"}"#)
        #expect(event == .exited)
    }

    @Test func decodesDetachedFrame() {
        let event = TerminalWire.decodeOutput(#"{"type": "detached"}"#)
        #expect(event == .detached)
    }

    @Test func decodesAttachedFrameAsOther() {
        // "attached" is the real first frame the daemon sends on connect
        // (`send_json(sock, {"type": "attached", "session_id": session_id})`)
        // but it isn't one of the three cases the terminal view acts on.
        let frame = #"{"type": "attached", "session_id": "s1"}"#
        let event = TerminalWire.decodeOutput(frame)
        #expect(event == .other(frame))
    }

    @Test func decodesGarbageAsOther() {
        let frame = "not json at all"
        let event = TerminalWire.decodeOutput(frame)
        #expect(event == .other(frame))
    }

    @Test func decodesUnknownTypeAsOther() {
        let frame = #"{"type": "pong"}"#
        let event = TerminalWire.decodeOutput(frame)
        #expect(event == .other(frame))
    }

    private func decodeJSON(_ string: String) -> [String: Any]? {
        guard let data = string.data(using: .utf8) else { return nil }
        return try? JSONSerialization.jsonObject(with: data) as? [String: Any]
    }
}
