import SwiftUI
import SwiftTerm
import NexusKit

/// PTY escape hatch: renders a live shell session over the harness's
/// terminal WebSocket using SwiftTerm. All wire handling and socket
/// lifecycle live in `TerminalBridge`; this file is purely the SwiftUI/
/// UIKit glue plus a small "session ended" overlay.
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
    let client: NexusClient
    let sessionID: String

    /// Set once, on the main actor, by `TerminalBridge.onSessionEnded` — the
    /// remote process exited, the daemon detached us, or the socket simply
    /// dropped. Drawn as a dismissible overlay rather than an automatic pop
    /// so the user can still read whatever last output is on screen.
    @State private var sessionEnded = false

    var body: some View {
        TerminalRepresentable(client: client, sessionID: sessionID, onSessionEnded: { sessionEnded = true })
            .navigationTitle("Terminal")
            .navigationBarTitleDisplayMode(.inline)
            .ignoresSafeArea(.container, edges: .bottom)
            .overlay {
                if sessionEnded {
                    SessionEndedOverlay()
                }
            }
    }
}

private struct SessionEndedOverlay: View {
    var body: some View {
        VStack(spacing: 8) {
            Text("Session ended")
                .font(.headline)
            Text("The terminal session was closed.")
                .font(.subheadline)
                .foregroundStyle(.secondary)
        }
        .padding()
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 12))
        .padding()
        .accessibilityIdentifier("terminal-session-ended")
    }
}

private struct TerminalRepresentable: UIViewRepresentable {
    let client: NexusClient
    let sessionID: String
    let onSessionEnded: () -> Void

    func makeCoordinator() -> TerminalBridge {
        TerminalBridge(request: client.terminalRequest(sessionID: sessionID))
    }

    func makeUIView(context: Context) -> SwiftTerm.TerminalView {
        let view = SwiftTerm.TerminalView(frame: .zero, font: nil)
        view.terminalDelegate = context.coordinator
        // Dark background/foreground to roughly match the web client, using
        // only the two colors SwiftTerm exposes as plain UIColor properties
        // (its 16-entry ANSI `installColors` palette takes its own `Color`
        // type and isn't worth the extra conversion code for this screen).
        view.nativeBackgroundColor = UIColor(red: 0.02, green: 0.03, blue: 0.05, alpha: 1.0)
        view.nativeForegroundColor = UIColor(red: 0.86, green: 0.91, blue: 0.95, alpha: 1.0)
        context.coordinator.onSessionEnded = onSessionEnded
        context.coordinator.attach(view)
        DispatchQueue.main.async { view.becomeFirstResponder() }
        return view
    }

    func updateUIView(_ uiView: SwiftTerm.TerminalView, context: Context) {}

    static func dismantleUIView(_ uiView: SwiftTerm.TerminalView, coordinator: TerminalBridge) {
        coordinator.detach()
    }
}
