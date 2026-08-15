import Foundation
import SwiftTerm
import DroverKit
import UIKit

/// Bridges SwiftTerm's `TerminalView` to the harness's terminal WebSocket:
/// pumps `TerminalStream`'s events (output/exit/connection state) into the
/// terminal, and forwards keystrokes/resizes back out as `input`/`resize`
/// frames. Also doubles as the `UIViewRepresentable`'s `Coordinator` (see
/// `TerminalView.swift`).
///
/// All socket lifecycle — including reconnecting with backoff after a drop —
/// lives in `TerminalStream` (DroverKit, unit-tested). The daemon keeps the
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
/// same way the test-only fakes in DroverKitTests are: a class handed across
/// an isolation boundary with manually-verified safe access patterns rather
/// than actor isolation.
final class TerminalBridge: NSObject, TerminalViewDelegate, @unchecked Sendable {
    private let stream: TerminalStream
    private weak var terminalView: SwiftTerm.TerminalView?
    private var pumpTask: Task<Void, Never>?
    private weak var navigationGesture: UILongPressGestureRecognizer?
    private weak var navigationOverlay: TerminalDirectionOverlay?
    private var navigationTimer: Timer?
    private var navigationOrigin = CGPoint.zero
    private var navigationRepeater = TerminalNavigationRepeater()

    /// Fired once, always on the main actor, when the remote process exits
    /// (the daemon's `exit` frame). Socket drops no longer fire this — they
    /// reconnect instead (see `onConnectionChanged`).
    var onSessionEnded: (() -> Void)?
    /// Fired on the main actor whenever the connection comes up or drops —
    /// drives the "Reconnecting…" pill.
    var onConnectionChanged: ((Bool) -> Void)?
    /// Fired on the main actor when an attempt ended without ever attaching,
    /// with the reason. Separate from `onConnectionChanged(false)`, which also
    /// fires for a drop after the terminal was live: only a never-attached
    /// attempt says the session has never been reachable, and only that case
    /// leaves a black screen with nothing on it to explain itself (#170).
    var onConnectFailed: ((String) -> Void)?

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
        navigationTimer?.invalidate()
        navigationTimer = nil
        navigationRepeater.stop()

        let gesture = navigationGesture
        let overlay = navigationOverlay
        navigationGesture = nil
        navigationOverlay = nil
        Task { @MainActor [weak view = terminalView, weak gesture, weak overlay] in
            if let gesture {
                view?.removeGestureRecognizer(gesture)
            }
            overlay?.removeFromSuperview()
        }
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

    /// Sends the daemon's `interrupt` frame (Ctrl-C into the PTY) — the
    /// toolbar's one-tap alternative to the accessory bar's Ctrl-toggle+C
    /// two-tap sequence.
    func sendInterrupt() {
        stream.send(TerminalWire.interruptFrame())
    }

    /// Types the iOS clipboard into the PTY as one `input` frame — raw text,
    /// newlines included (Termius behavior; no bracketed paste). Empty or
    /// non-text clipboard is a no-op.
    @MainActor
    func sendPaste() {
        guard let text = UIPasteboard.general.string, !text.isEmpty else { return }
        stream.send(TerminalWire.inputFrame(text))
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
        case .connectFailed(let reason):
            onConnectFailed?(reason)
        }
    }

    // MARK: - TerminalViewDelegate

    // `send`, `sizeChanged`, and `clipboardCopy` are the only ones that do
    // anything: the first two produce this protocol's outgoing wire frames,
    // and clipboardCopy bridges the user's selection to UIPasteboard. The
    // rest of the protocol (title/cwd updates, scroll position, link taps,
    // bell, iTerm content, damage-region reporting) has nothing to do with
    // the harness's terminal wire protocol, so they're deliberate no-ops
    // rather than left unimplemented (the protocol has no default for most
    // of them on iOS).

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
    // SwiftTerm calls this with the UTF-8 bytes of the current selection when
    // the user picks Copy. Delegate callbacks carry no actor isolation
    // (see the class comment), so hop before touching UIPasteboard.
    func clipboardCopy(source: SwiftTerm.TerminalView, content: Data) {
        guard let text = String(data: content, encoding: .utf8), !text.isEmpty else { return }
        Task { @MainActor in
            UIPasteboard.general.string = text
        }
    }
    func rangeChanged(source: SwiftTerm.TerminalView, startY: Int, endY: Int) {}

    // MARK: - Touch navigation

    /// Installs Termius-style hold-and-drag arrow navigation. SwiftTerm's own
    /// long press opens its selection menu; the product choice here is to make
    /// a hold exclusively directional while leaving double-tap selection
    /// intact. Ordinary pans still scroll immediately because the hold must
    /// recognize before the finger starts moving.
    @MainActor
    func installNavigationGesture(on view: SwiftTerm.TerminalView) {
        let gesture = UILongPressGestureRecognizer(
            target: self,
            action: #selector(handleNavigationGesture(_:)))
        gesture.minimumPressDuration = 0.35
        gesture.allowableMovement = 14
        gesture.cancelsTouchesInView = true

        // The custom hold wins explicitly over SwiftTerm's built-in 0.7s
        // context-menu hold. Double-tap selection/copy is a separate gesture
        // and remains available.
        view.gestureRecognizers?
            .compactMap { $0 as? UILongPressGestureRecognizer }
            .forEach { $0.require(toFail: gesture) }
        view.addGestureRecognizer(gesture)
        navigationGesture = gesture
    }

    @objc func handleNavigationGesture(_ gesture: UILongPressGestureRecognizer) {
        MainActor.assumeIsolated {
            guard let view = terminalView else { return }
            switch gesture.state {
            case .began:
                navigationOrigin = gesture.location(in: view)
                navigationRepeater.stop()
                showNavigationOverlay(in: view)
            case .changed:
                let location = gesture.location(in: view)
                updateNavigation(
                    horizontal: location.x - navigationOrigin.x,
                    vertical: location.y - navigationOrigin.y)
            case .ended, .cancelled, .failed:
                stopNavigationGesture()
            default:
                break
            }
        }
    }

    @MainActor
    private func updateNavigation(horizontal: CGFloat, vertical: CGFloat) {
        let previous = navigationRepeater.motion
        let immediate = navigationRepeater.update(
            horizontal: Double(horizontal),
            vertical: Double(vertical))
        let current = navigationRepeater.motion
        guard current != previous else { return }

        navigationOverlay?.setMotion(current)
        navigationTimer?.invalidate()
        navigationTimer = nil

        if let immediate {
            sendArrow(immediate)
            UISelectionFeedbackGenerator().selectionChanged()
        }
        guard let current else { return }

        let timer = Timer(timeInterval: current.repeatInterval, repeats: true) {
            [weak self] _ in
            MainActor.assumeIsolated {
                guard let self,
                      let direction = self.navigationRepeater.repeatedDirection()
                else { return }
                self.sendArrow(direction)
            }
        }
        timer.tolerance = min(current.repeatInterval * 0.1, 0.01)
        // Default-mode timers pause while UIKit tracks a finger. Common mode
        // is load-bearing: the arrows must repeat during the held gesture.
        RunLoop.main.add(timer, forMode: .common)
        navigationTimer = timer
    }

    @MainActor
    private func sendArrow(_ direction: TerminalNavigationDirection) {
        guard let view = terminalView else { return }
        let applicationCursor = view.getTerminal().applicationCursor
        let bytes: [UInt8]
        switch direction {
        case .up:
            bytes = applicationCursor
                ? EscapeSequences.moveUpApp : EscapeSequences.moveUpNormal
        case .down:
            bytes = applicationCursor
                ? EscapeSequences.moveDownApp : EscapeSequences.moveDownNormal
        case .left:
            bytes = applicationCursor
                ? EscapeSequences.moveLeftApp : EscapeSequences.moveLeftNormal
        case .right:
            bytes = applicationCursor
                ? EscapeSequences.moveRightApp : EscapeSequences.moveRightNormal
        }
        view.send(bytes)
    }

    @MainActor
    private func showNavigationOverlay(in view: UIView) {
        navigationOverlay?.removeFromSuperview()
        let overlay = TerminalDirectionOverlay(frame: CGRect(x: 0, y: 0,
                                                              width: 104, height: 104))
        overlay.center = CGPoint(x: max(64, view.bounds.maxX - 64), y: 72)
        overlay.autoresizingMask = [.flexibleLeftMargin, .flexibleBottomMargin]
        view.addSubview(overlay)
        navigationOverlay = overlay
        overlay.show()
    }

    @MainActor
    private func stopNavigationGesture() {
        navigationTimer?.invalidate()
        navigationTimer = nil
        navigationRepeater.stop()
        navigationOverlay?.hideAndRemove()
        navigationOverlay = nil
    }

    // MARK: - Font size (pinch-zoom)

    /// Persisted terminal font size. Clamped on read so a corrupted default
    /// can't produce an unusable terminal; 0 (key absent) → default.
    private static let fontSizeKey = "terminalFontSize"
    static let fontSizeRange: ClosedRange<CGFloat> = 9...24
    static let defaultFontSize: CGFloat = 13

    static var storedFontSize: CGFloat {
        let raw = CGFloat(UserDefaults.standard.double(forKey: fontSizeKey))
        guard raw > 0 else { return defaultFontSize }
        return min(max(raw, fontSizeRange.lowerBound), fontSizeRange.upperBound)
    }

    /// Base size captured at gesture start so scale applies to where the
    /// pinch began, not to a moving target.
    private var pinchBaseFontSize: CGFloat = TerminalBridge.defaultFontSize

    // Gesture recognizers always fire on the main thread; this class isn't
    // MainActor (see class comment), so assume rather than hop.
    @objc func handlePinch(_ gesture: UIPinchGestureRecognizer) {
        MainActor.assumeIsolated {
            guard let view = terminalView else { return }
            switch gesture.state {
            case .began:
                pinchBaseFontSize = view.font.pointSize
            case .changed:
                let target = min(max(pinchBaseFontSize * gesture.scale,
                                     Self.fontSizeRange.lowerBound),
                                 Self.fontSizeRange.upperBound)
                // Font assignment rebuilds SwiftTerm's font set and relays
                // out the grid — skip sub-half-point changes to keep the
                // gesture smooth.
                if abs(target - view.font.pointSize) >= 0.5 {
                    view.font = UIFont.monospacedSystemFont(ofSize: target, weight: .regular)
                }
            case .ended, .cancelled:
                UserDefaults.standard.set(Double(view.font.pointSize), forKey: Self.fontSizeKey)
            default:
                break
            }
        }
    }
}
