import SwiftUI
import SwiftTerm
import DroverKit

/// PTY escape hatch: renders a live shell session over the harness's
/// terminal WebSocket using SwiftTerm. All wire handling and socket
/// lifecycle live in `TerminalBridge`/`TerminalStream`; this file is purely
/// the SwiftUI/UIKit glue plus the "session ended" overlay and the
/// "Reconnecting…" pill.
///
/// The Ctrl-key accessory bar the brief calls for (Esc, Tab, Ctrl-C, arrows)
/// is not hand-built here: SwiftTerm's `TerminalView` wires up its own
/// `TerminalAccessory` automatically during init (`setupAccessoryView()`,
/// called from the iOS `init(frame:font:)`) and assigns it as
/// `inputAccessoryView` — it already covers Esc, Tab, a Ctrl modifier
/// toggle, and auto-repeating arrow keys, all funneling through the same
/// `terminalDelegate.send(source:data:)` this bridge already implements.
/// Reusing it means one less hand-rolled UIKit control to maintain; Ctrl-C
/// is a two-tap sequence (toggle Ctrl, tap C) rather than a single button,
/// which matches how other iOS terminal clients (Termius, Blink) expose it.
struct TerminalScreen: View {
    let client: DroverClient
    let sessionID: String
    let harness: String?

    @Environment(\.scenePhase) private var scenePhase
    @Environment(\.dismiss) private var dismiss

    /// Set once, on the main actor, by `TerminalBridge.onSessionEnded` — the
    /// remote process really exited (the daemon's `exit` frame). Socket
    /// drops no longer end the session; they show `isReconnecting` instead
    /// while `TerminalStream` reattaches. Drawn as a dismissible overlay
    /// rather than an automatic pop so the user can still read whatever last
    /// output is on screen.
    @State private var sessionEnded = false
    /// True while the terminal socket is down and the stream is retrying.
    /// Suppressed during the initial connect (nothing to *re*-connect to
    /// yet) by only flipping on after the first successful connection —
    /// `TerminalStream` emits `.connection(true)` before any drop can.
    @State private var isReconnecting = false
    @State private var hasConnectedOnce = false
    @State private var bridgeHolder = BridgeHolder()
    @State private var showTerminateConfirm = false
    @State private var terminateHint: String?

    init(client: DroverClient, sessionID: String, harness: String? = nil) {
        self.client = client
        self.sessionID = sessionID
        self.harness = harness
    }

    var body: some View {
        let presentation = HarnessPresentation(harness ?? "shell")
        VStack(spacing: 0) {
            if hasConnectedOnce && isReconnecting && !sessionEnded {
                ReconnectingPill(accessibilityID: "terminal-reconnecting")
            }
            if let terminateHint {
                Text(terminateHint)
                    .font(.caption)
                    .foregroundStyle(.orange)
                    .padding(.vertical, 4)
            }
            TerminalRepresentable(
                client: client,
                sessionID: sessionID,
                holder: bridgeHolder,
                onSessionEnded: { sessionEnded = true },
                onConnectionChanged: { up in
                    if up { hasConnectedOnce = true }
                    isReconnecting = !up
                }
            )
        }
        .navigationTitle(presentation.name)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar { toolbarContent(presentation: presentation) }
        .confirmationDialog("Terminate this session?", isPresented: $showTerminateConfirm,
                            titleVisibility: .visible) {
            Button("Terminate", role: .destructive) {
                Task { await terminateSession() }
            }
        }
        .ignoresSafeArea(.container, edges: .bottom)
        .overlay {
            if sessionEnded {
                SessionEndedOverlay { dismiss() }
            }
        }
        // Returning to the foreground: iOS suspended the socket while
        // backgrounded, and the stream may be mid-backoff. Nudge it so the
        // reattach happens now instead of after up to 30s.
        .onChange(of: scenePhase) { _, phase in
            if phase == .active {
                bridgeHolder.bridge?.reconnectNow()
            }
        }
    }

    /// Escape hatches for a wedged CLI (e.g. a self-update that hung the
    /// process): one-tap Ctrl-C over the WebSocket, and a real terminate
    /// (SIGTERM→SIGKILL to the process group) over REST. Parity with the
    /// web client's Ctrl-C/Kill buttons. Also hosts the Paste menu item for
    /// pushing clipboard text into the session.
    @ToolbarContentBuilder
    private func toolbarContent(presentation: HarnessPresentation) -> some ToolbarContent {
        ToolbarItem(placement: .principal) {
            Label(presentation.name, systemImage: presentation.symbolName)
                .labelStyle(.titleAndIcon)
                .font(.headline)
                .accessibilityIdentifier("terminal-harness-title")
        }

        ToolbarItem(placement: .topBarTrailing) {
            Menu {
                Button {
                    bridgeHolder.bridge?.sendPaste()
                } label: {
                    Label("Paste", systemImage: "doc.on.clipboard")
                }
                Button {
                    bridgeHolder.bridge?.sendInterrupt()
                } label: {
                    Label("Interrupt (Ctrl-C)", systemImage: "stop.circle")
                }
                Button(role: .destructive) {
                    showTerminateConfirm = true
                } label: {
                    Label("Terminate", systemImage: "xmark.octagon")
                }
            } label: {
                Image(systemName: "ellipsis.circle")
            }
            .accessibilityLabel("Terminal actions")
            .accessibilityIdentifier("terminal-menu")
        }
    }

    private func terminateSession() async {
        do {
            try await client.terminate(sessionID: sessionID)
            // The daemon sends an `exit` frame to attached clients, which
            // flips the overlay; if none arrives (already-gone session on a
            // restarted daemon), don't leave the user staring at a frozen
            // screen — just leave.
            try? await Task.sleep(for: .seconds(2))
            if !sessionEnded { dismiss() }
        } catch {
            terminateHint = "Could not terminate — try again."
        }
    }
}

/// Lets the SwiftUI screen reach the representable's coordinator (for the
/// foreground `reconnectNow()` nudge) without owning its lifecycle.
@MainActor
private final class BridgeHolder {
    weak var bridge: TerminalBridge?
}

private struct SessionEndedOverlay: View {
    let onClose: () -> Void

    var body: some View {
        VStack(spacing: 12) {
            Text("Session ended")
                .font(.headline)
            Text("The terminal session was closed.")
                .font(.subheadline)
                .foregroundStyle(.secondary)
            Button("Close") { onClose() }
                .buttonStyle(.borderedProminent)
                .accessibilityIdentifier("terminal-session-ended-close")
        }
        .padding()
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 12))
        .padding()
        .accessibilityIdentifier("terminal-session-ended")
    }
}

private struct TerminalRepresentable: UIViewRepresentable {
    let client: DroverClient
    let sessionID: String
    let holder: BridgeHolder
    let onSessionEnded: () -> Void
    let onConnectionChanged: (Bool) -> Void

    func makeCoordinator() -> TerminalBridge {
        TerminalBridge(request: client.terminalRequest(sessionID: sessionID))
    }

    func makeUIView(context: Context) -> SwiftTerm.TerminalView {
        let view = SwiftTerm.TerminalView(
            frame: .zero,
            font: UIFont.monospacedSystemFont(ofSize: TerminalBridge.storedFontSize,
                                              weight: .regular))
        view.terminalDelegate = context.coordinator
        // Dark background/foreground to roughly match the web client, using
        // only the two colors SwiftTerm exposes as plain UIColor properties
        // (its 16-entry ANSI `installColors` palette takes its own `Color`
        // type and isn't worth the extra conversion code for this screen).
        view.nativeBackgroundColor = UIColor(red: 0.02, green: 0.03, blue: 0.05, alpha: 1.0)
        view.nativeForegroundColor = UIColor(red: 0.86, green: 0.91, blue: 0.95, alpha: 1.0)
        context.coordinator.onSessionEnded = onSessionEnded
        context.coordinator.onConnectionChanged = onConnectionChanged
        holder.bridge = context.coordinator
        context.coordinator.attach(view)
        context.coordinator.installNavigationGesture(on: view)
        let pinch = UIPinchGestureRecognizer(
            target: context.coordinator,
            action: #selector(TerminalBridge.handlePinch(_:)))
        view.addGestureRecognizer(pinch)
        DispatchQueue.main.async { view.becomeFirstResponder() }
        return view
    }

    func updateUIView(_ uiView: SwiftTerm.TerminalView, context: Context) {}

    static func dismantleUIView(_ uiView: SwiftTerm.TerminalView, coordinator: TerminalBridge) {
        coordinator.detach()
    }
}
