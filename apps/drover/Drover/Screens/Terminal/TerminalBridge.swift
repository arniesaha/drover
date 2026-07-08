import Foundation
import SwiftTerm
import NexusKit

/// Bridges SwiftTerm's `TerminalView` to the harness's terminal WebSocket
/// (`NexusClient.terminalRequest`): runs the `URLSessionWebSocketTask`
/// receive loop, decodes frames via `TerminalWire`, feeds `output` text into
/// the terminal, and forwards keystrokes/resizes back out as `input`/
/// `resize` frames. Also doubles as the `UIViewRepresentable`'s
/// `Coordinator` (see `TerminalView.swift`).
///
/// SwiftTerm's `TerminalViewDelegate` protocol carries no actor isolation,
/// so nothing here can assume its callbacks land on the main actor even
/// though in practice UIKit invokes them from the main run loop. The
/// WebSocket receive loop runs on a plain background `Task` by
/// construction, so every touch of the (weak, main-actor-isolated)
/// `TerminalView` is explicitly hopped via `@MainActor` — see `handle(_:)`
/// and the `onSessionEnded` invocation in the catch branch of
/// `runReceiveLoop()`. This class is `@unchecked Sendable` the same way the
/// test-only `FakeConnector` in `NexusKitTests/StreamTests.swift` is: a
/// class handed across an isolation boundary with manually-verified safe
/// access patterns rather than actor isolation.
final class TerminalBridge: NSObject, TerminalViewDelegate, @unchecked Sendable {
    private let webSocketTask: URLSessionWebSocketTask
    private weak var terminalView: SwiftTerm.TerminalView?
    private var receiveTask: Task<Void, Never>?

    /// Fired once, always on the main actor, when the remote process exits
    /// (`TerminalEvent.exited`), the daemon sends a detach frame
    /// (`TerminalEvent.detached` — not currently sent by the deployed
    /// daemon, but decoded defensively), or the WebSocket simply closes/
    /// errors (the daemon's actual way of signaling a detach).
    var onSessionEnded: (() -> Void)?

    init(request: URLRequest, urlSession: URLSession = URLSession(configuration: .default)) {
        webSocketTask = urlSession.webSocketTask(with: request)
        super.init()
    }

    /// Belt-and-suspenders teardown uniformity: every other socket/task
    /// owner in this app (ChatModel, MessageStream) cancels its work in
    /// `deinit` even though callers are also expected to call the explicit
    /// teardown method. Guards against a dropped `TerminalBridge` (e.g. the
    /// owning view going away without `dismantleUIView` running) leaking the
    /// WebSocket and receive loop.
    deinit {
        detach()
    }

    /// Starts the socket and its receive loop. Call once, right after
    /// `makeUIView` creates the terminal.
    func attach(_ terminalView: SwiftTerm.TerminalView) {
        self.terminalView = terminalView
        webSocketTask.resume()
        receiveTask = Task { [weak self] in
            await self?.runReceiveLoop()
        }
    }

    /// Tears down the socket and cancels the receive loop. Call from
    /// `UIViewRepresentable.dismantleUIView` so leaving the screen doesn't
    /// leak an open connection to the harness.
    func detach() {
        receiveTask?.cancel()
        receiveTask = nil
        webSocketTask.cancel(with: .goingAway, reason: nil)
    }

    // MARK: - Receive loop

    private func runReceiveLoop() async {
        while !Task.isCancelled {
            let message: URLSessionWebSocketTask.Message
            do {
                message = try await webSocketTask.receive()
            } catch {
                // Socket closed or errored — the daemon's only actual way
                // of signaling a detach (it never sends a "detached" JSON
                // frame; see TerminalWire's doc comment).
                await MainActor.run { [weak self] in self?.onSessionEnded?() }
                return
            }

            let frame: String?
            switch message {
            case .string(let text):
                frame = text
            case .data(let data):
                frame = String(data: data, encoding: .utf8)
            @unknown default:
                frame = nil
            }
            guard let frame, let event = TerminalWire.decodeOutput(frame) else { continue }
            await handle(event)
        }
    }

    @MainActor
    private func handle(_ event: TerminalEvent) {
        switch event {
        case .output(let text):
            terminalView?.feed(text: text)
        case .exited, .detached:
            onSessionEnded?()
        case .other:
            break  // "attached"/"event"/"error"/"pong" chatter — not rendered
        }
    }

    // MARK: - Outgoing frames

    private func sendFrame(_ frame: String) {
        webSocketTask.send(.string(frame)) { _ in }
    }

    // MARK: - TerminalViewDelegate

    // Only `send` and `sizeChanged` do anything: those are the two outgoing
    // wire frames this protocol exists to produce. The rest of the protocol
    // (title/cwd updates, scroll position, link taps, clipboard, bell,
    // iTerm content, damage-region reporting) has nothing to do with the
    // harness's terminal wire protocol, so they're deliberate no-ops rather
    // than left unimplemented (the protocol has no default for most of
    // them on iOS).

    func send(source: SwiftTerm.TerminalView, data: ArraySlice<UInt8>) {
        sendFrame(TerminalWire.inputFrame(String(decoding: data, as: UTF8.self)))
    }

    func sizeChanged(source: SwiftTerm.TerminalView, newCols: Int, newRows: Int) {
        sendFrame(TerminalWire.resizeFrame(rows: newRows, cols: newCols))
    }

    func setTerminalTitle(source: SwiftTerm.TerminalView, title: String) {}
    func hostCurrentDirectoryUpdate(source: SwiftTerm.TerminalView, directory: String?) {}
    func scrolled(source: SwiftTerm.TerminalView, position: Double) {}
    func requestOpenLink(source: SwiftTerm.TerminalView, link: String, params: [String: String]) {}
    func clipboardCopy(source: SwiftTerm.TerminalView, content: Data) {}
    func rangeChanged(source: SwiftTerm.TerminalView, startY: Int, endY: Int) {}
}
