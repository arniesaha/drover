import Foundation
import SwiftTerm
import NexusKit

/// Bridges SwiftTerm's `TerminalView` to the harness's terminal WebSocket:
/// pumps `TerminalStream`'s events (output/exit/connection state) into the
/// terminal, and forwards keystrokes/resizes back out as `input`/`resize`
/// frames. Also doubles as the `UIViewRepresentable`'s `Coordinator` (see
/// `TerminalView.swift`).
///
/// All socket lifecycle — including reconnecting with backoff after a drop —
/// lives in `TerminalStream` (NexusKit, unit-tested). The daemon keeps the
/// PTY alive when a client vanishes, so a network blip or iOS suspending the
/// socket in the background is survivable: the stream reattaches and the
/// re-sent resize makes full-screen TUIs repaint. Only the daemon's `exit`
/// frame (the process really died) ends the session for good.
///
/// SwiftTerm's `TerminalViewDelegate` protocol carries no actor isolation,
/// so nothing here can assume its callbacks land on the main actor even
/// though in practice UIKit invokes them from the main run loop. The event
/// pump runs on a plain background `Task` by construction, so every touch of
/// the (weak, main-actor-isolated) `TerminalView` is explicitly hopped via
/// `@MainActor` — see `apply(_:)`. This class is `@unchecked Sendable` the
/// same way the test-only fakes in NexusKitTests are: a class handed across
/// an isolation boundary with manually-verified safe access patterns rather
/// than actor isolation.
final class TerminalBridge: NSObject, TerminalViewDelegate, @unchecked Sendable {
    private let stream: TerminalStream
    private weak var terminalView: SwiftTerm.TerminalView?
    private var pumpTask: Task<Void, Never>?

    /// Fired once, always on the main actor, when the remote process exits
    /// (the daemon's `exit` frame). Socket drops no longer fire this — they
    /// reconnect instead (see `onConnectionChanged`).
    var onSessionEnded: (() -> Void)?
    /// Fired on the main actor whenever the connection comes up or drops —
    /// drives the "Reconnecting…" pill.
    var onConnectionChanged: ((Bool) -> Void)?

    init(request: URLRequest) {
        stream = TerminalStream(request: request)
        super.init()
    }

    /// Belt-and-suspenders teardown uniformity: every other socket/task
    /// owner in this app (ChatModel, MessageStream) cancels its work in
    /// `deinit` even though callers are also expected to call the explicit
    /// teardown method. Guards against a dropped `TerminalBridge` (e.g. the
    /// owning view going away without `dismantleUIView` running) leaking the
    /// WebSocket and pump.
    deinit {
        detach()
    }

    /// Starts the stream and its event pump. Call once, right after
    /// `makeUIView` creates the terminal.
    func attach(_ terminalView: SwiftTerm.TerminalView) {
        self.terminalView = terminalView
        pumpTask = Task { [weak self] in
            guard let stream = self?.stream else { return }
            for await event in await stream.events() {
                guard !Task.isCancelled, let self else { break }
                await MainActor.run { self.apply(event) }
            }
        }
    }

    /// Tears down the socket and cancels the pump. Call from
    /// `UIViewRepresentable.dismantleUIView` so leaving the screen doesn't
    /// leak an open connection to the harness.
    func detach() {
        pumpTask?.cancel()
        pumpTask = nil
        let stream = stream
        Task { await stream.stop() }
    }

    /// Wakes a pending reconnect backoff — called when the app returns to
    /// the foreground so a suspended terminal reattaches immediately instead
    /// of waiting out up to 30s of backoff.
    func reconnectNow() {
        let stream = stream
        Task { await stream.nudge() }
    }

    @MainActor
    private func apply(_ event: TerminalStreamEvent) {
        switch event {
        case .output(let text):
            terminalView?.feed(text: text)
        case .exited:
            onSessionEnded?()
        case .connection(let up):
            onConnectionChanged?(up)
        }
    }

    // MARK: - TerminalViewDelegate

    // Only `send` and `sizeChanged` do anything: those are the two outgoing
    // wire frames this protocol exists to produce. The rest of the protocol
    // (title/cwd updates, scroll position, link taps, clipboard, bell,
    // iTerm content, damage-region reporting) has nothing to do with the
    // harness's terminal wire protocol, so they're deliberate no-ops rather
    // than left unimplemented (the protocol has no default for most of
    // them on iOS).

    // Synchronous by design: TerminalStream.send/sendResize are nonisolated,
    // so per-keystroke calls stay in order (an unstructured Task per
    // keystroke would not).

    func send(source: SwiftTerm.TerminalView, data: ArraySlice<UInt8>) {
        stream.send(TerminalWire.inputFrame(String(decoding: data, as: UTF8.self)))
    }

    func sizeChanged(source: SwiftTerm.TerminalView, newCols: Int, newRows: Int) {
        stream.sendResize(rows: newRows, cols: newCols)
    }

    func setTerminalTitle(source: SwiftTerm.TerminalView, title: String) {}
    func hostCurrentDirectoryUpdate(source: SwiftTerm.TerminalView, directory: String?) {}
    func scrolled(source: SwiftTerm.TerminalView, position: Double) {}
    func requestOpenLink(source: SwiftTerm.TerminalView, link: String, params: [String: String]) {}
    func clipboardCopy(source: SwiftTerm.TerminalView, content: Data) {}
    func rangeChanged(source: SwiftTerm.TerminalView, startY: Int, endY: Int) {}
}
