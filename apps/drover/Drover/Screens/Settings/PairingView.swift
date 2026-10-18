import AVFoundation
import SwiftUI
import DroverKit

/// Scan-to-pair.
///
/// The camera is the happy path. Manual entry exists because a camera can be
/// denied, broken, or pointed at a terminal whose QR will not scan, and being
/// locked out of your own fleet over that would be absurd. Both routes build
/// the same `PairingPayload`, so there is exactly one code path below them.
@MainActor
final class PairingModel: ObservableObject {
    @Published var serverURLString: String
    @Published var manualCode: String = ""
    @Published var statusMessage: String?
    @Published var statusIsError = false
    @Published var isPairing = false
    @Published private(set) var requiresHTTPSRePairing = false

    static let transportGuideURL = URL(
        string: "https://github.com/arniesaha/drover/blob/main/docs/getting-started.md#remote-tailscale-https"
    )!

    init(serverURLString: String = "") {
        self.serverURLString = serverURLString
    }

    func prefill(_ payload: PairingPayload) {
        serverURLString = payload.serverURL.absoluteString
        manualCode = payload.code
        _ = allowsPairing(payload)
    }

    /// Reject before redeeming a single-use pairing code. Waiting for ATS to
    /// reject the network request would burn the code and leave no actionable
    /// recovery path.
    func allowsPairing(_ payload: PairingPayload) -> Bool {
        let requiresHTTPS = ServerConfig(urlString: payload.serverURL.absoluteString)?
            .requiresHTTPSForAppTransportSecurity == true
        requiresHTTPSRePairing = requiresHTTPS
        if requiresHTTPS {
            statusMessage = ServerConfig.httpsRePairMessage
            statusIsError = true
            return false
        }
        if statusMessage == ServerConfig.httpsRePairMessage {
            statusMessage = nil
            statusIsError = false
        }
        return true
    }

    var canSubmitManualCode: Bool {
        manualPayload() != nil
    }

    /// Manual entry produces exactly the payload a scan would.
    func manualPayload() -> PairingPayload? {
        let code = manualCode.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !code.isEmpty else { return nil }

        var authority = serverURLString.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !authority.isEmpty else { return nil }
        var tls = false
        for scheme in ["https://", "http://"] where authority.hasPrefix(scheme) {
            tls = scheme == "https://"
            authority = String(authority.dropFirst(scheme.count))
        }
        authority = authority.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard !authority.isEmpty else { return nil }

        let allowed = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-"))
        let escaped = code.addingPercentEncoding(withAllowedCharacters: allowed) ?? code
        let suffix = tls ? "&tls=1" : ""
        return PairingPayload(
            scanned: "drover://\(authority)?v=1&code=\(escaped)\(suffix)"
        )
    }
}

struct PairingView: View {
    var environment: AppEnvironment
    var onPaired: () -> Void = {}

    var initialPayload: PairingPayload? = nil
    @State private var pendingPayload: PairingPayload?
    @State private var showPairConfirmation = false
    @State private var scanGeneration = 0
    @StateObject private var model = PairingModel()
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                if initialPayload == nil {
                    QRScannerView { scanned in
                        guard !model.isPairing, let payload = PairingPayload(scanned: scanned) else { return }
                        model.prefill(payload)
                        guard model.allowsPairing(payload) else { return }
                        pendingPayload = payload
                        showPairConfirmation = true
                    }
                    .id(scanGeneration)
                    .frame(height: 260)
                    .frame(maxWidth: .infinity)
                    .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
                    .overlay {
                        RoundedRectangle(cornerRadius: 14, style: .continuous)
                            .strokeBorder(DroverColor.line, lineWidth: 1)
                    }

                    Text("Run `drover-server pair` on your Drover machine, then scan the code it prints.")
                        .droverText(.subtitle)
                        .foregroundStyle(DroverColor.faint)
                        .fixedSize(horizontal: false, vertical: true)

                }

                Divider().overlay(DroverColor.line)

                Text(initialPayload == nil ? "Or enter it by hand" : "Review pairing details").droverText(.h3)

                field("http://host:7080", text: $model.serverURLString)
                    .keyboardType(.URL)
                field("K7QP-2M4X", text: $model.manualCode)
                    .textInputAutocapitalization(.characters)

                if let statusMessage = model.statusMessage {
                    statusRow(statusMessage)
                }
                if model.requiresHTTPSRePairing {
                    Link("Remote HTTPS setup guide", destination: PairingModel.transportGuideURL)
                        .droverText(.subtitle)
                        .foregroundStyle(DroverColor.accentHi)
                }
            }
            .padding(.horizontal, 18)
            .padding(.vertical, 14)
        }
        .safeAreaInset(edge: .bottom, spacing: 0) {
            Button {
                guard let payload = model.manualPayload() else { return }
                guard model.allowsPairing(payload) else { return }
                pendingPayload = payload
                showPairConfirmation = true
            } label: {
                Text(model.isPairing ? "Pairing..." : "Pair")
                    .droverText(.h3)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 12)
            }
            .disabled(!model.canSubmitManualCode || model.isPairing)
            .foregroundStyle(DroverColor.accentHi)
            .overlay {
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .strokeBorder(DroverColor.accentHi, lineWidth: 1)
            }
            .accessibilityIdentifier("pairing-submit")
            .padding(.horizontal, 18)
            .padding(.vertical, 10)
            .background(DroverColor.bg)
        }
        .background(DroverColor.bg)
        .scrollDismissesKeyboard(.interactively)
        .navigationTitle("Pair")
        .navigationBarTitleDisplayMode(.inline)
        .toolbarBackground(DroverColor.bg, for: .navigationBar)
        .toolbarBackground(.visible, for: .navigationBar)
        .confirmationDialog("Pair with this server?", isPresented: $showPairConfirmation,
                            titleVisibility: .visible) {
            Button("Pair") {
                guard let payload = pendingPayload else { return }
                pendingPayload = nil
                Task { await pair(payload) }
            }
            Button("Cancel", role: .cancel) {
                pendingPayload = nil
                scanGeneration += 1
            }
        } message: {
            Text(pendingPayload?.serverURL.absoluteString ?? "")
        }
        .onChange(of: initialPayload) { _, payload in
            guard !model.isPairing, let payload else { return }
            pendingPayload = nil
            showPairConfirmation = false
            model.prefill(payload)
        }
        .onAppear {
            if let initialPayload {
                model.prefill(initialPayload)
            }
            if model.serverURLString.isEmpty, let config = environment.config {
                model.serverURLString = config.baseURL.absoluteString
            }
        }
    }

    private func field(_ prompt: String, text: Binding<String>) -> some View {
        TextField(prompt, text: text)
            .textInputAutocapitalization(.never)
            .autocorrectionDisabled()
            .font(.system(.subheadline, design: .monospaced))
            .foregroundStyle(DroverColor.text)
            .padding(.horizontal, 13)
            .padding(.vertical, 12)
            .background(DroverColor.surface,
                        in: RoundedRectangle(cornerRadius: 10, style: .continuous))
            .overlay {
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .strokeBorder(DroverColor.line, lineWidth: 1)
            }
    }

    /// Same shape SettingsView uses, so a failure here reads the same as a
    /// failure there.
    private func statusRow(_ message: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 9) {
            Image(systemName: model.statusIsError
                  ? "exclamationmark.circle" : "checkmark.circle")
                .font(.system(size: 13, weight: .medium))
            Text(message).fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .droverText(.subtitle)
        .foregroundStyle(DroverColor.accentHi)
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(DroverColor.accentTint,
                    in: RoundedRectangle(cornerRadius: 10, style: .continuous))
        .accessibilityIdentifier(model.statusIsError ? "pairing-error" : "pairing-paired")
    }

    private func pair(_ payload: PairingPayload) async {
        guard !model.isPairing else { return }
        guard model.allowsPairing(payload) else { return }
        model.isPairing = true
        model.statusMessage = "Pairing..."
        model.statusIsError = false
        defer { model.isPairing = false }

        let response: PairResponse
        do {
            response = try await environment.pair(
                payload: payload,
                deviceName: UITestOverrides.pairingDeviceName(
                    fallback: UIDevice.current.name
                )
            )
        } catch {
            model.statusMessage = error.localizedDescription
            model.statusIsError = true
            return
        }

        // configure() re-validates against the live server before persisting
        // anything, so a token that pairs but cannot reach the hub still
        // fails loudly here rather than leaving a half-configured app.
        switch await environment.configure(
            urlString: payload.serverURL.absoluteString,
            token: response.token
        ) {
        case .success:
            model.statusMessage = "Paired with \(response.fleetName)."
            model.statusIsError = false
            onPaired()
            dismiss()
        case .failure(let detail):
            model.statusMessage = detail
            model.statusIsError = true
        }
    }
}

/// Thin AVFoundation wrapper: one metadata output, QR only.
struct QRScannerView: UIViewControllerRepresentable {
    var onScan: (String) -> Void

    func makeUIViewController(context: Context) -> QRScannerController {
        let controller = QRScannerController()
        controller.onScan = onScan
        return controller
    }

    func updateUIViewController(_ controller: QRScannerController, context: Context) {
        controller.onScan = onScan
    }
}

final class QRScannerController: UIViewController,
                                 AVCaptureMetadataOutputObjectsDelegate {
    var onScan: ((String) -> Void)?
    /// `nonisolated(unsafe)` because `AVCaptureSession` is not `Sendable` but
    /// is documented as safe to drive from one dedicated queue, which is
    /// exactly what `sessionQueue` below is. Configuration happens on the main
    /// actor in `viewDidLoad`; only start/stop run on the queue.
    nonisolated(unsafe) private let session = AVCaptureSession()
    private let sessionQueue = DispatchQueue(label: "drover.pairing.capture")
    private var preview: AVCaptureVideoPreviewLayer?
    private var hasScanned = false

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .black
        guard let device = AVCaptureDevice.default(for: .video),
              let input = try? AVCaptureDeviceInput(device: device),
              session.canAddInput(input)
        else { return }
        session.addInput(input)

        let output = AVCaptureMetadataOutput()
        guard session.canAddOutput(output) else { return }
        session.addOutput(output)
        output.setMetadataObjectsDelegate(self, queue: .main)
        output.metadataObjectTypes = [.qr]

        let layer = AVCaptureVideoPreviewLayer(session: session)
        layer.videoGravity = .resizeAspectFill
        view.layer.addSublayer(layer)
        preview = layer
    }

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        preview?.frame = view.bounds
    }

    override func viewWillAppear(_ animated: Bool) {
        super.viewWillAppear(animated)
        guard !session.isRunning else { return }
        // startRunning blocks, so it must not run on the main queue: doing so
        // stalls the push animation for as long as the camera takes to warm.
        sessionQueue.async { [session] in session.startRunning() }
    }

    override func viewWillDisappear(_ animated: Bool) {
        super.viewWillDisappear(animated)
        sessionQueue.async { [session] in session.stopRunning() }
    }

    /// `nonisolated` because the protocol is: a `UIViewController` is
    /// MainActor-isolated, so under Swift 6 the conformance cannot be.
    ///
    /// `assumeIsolated` rather than a `Task` hop, and it is sound for one
    /// specific reason: `viewDidLoad` registers this delegate with
    /// `queue: .main`, so every callback genuinely arrives on the main queue.
    /// Hopping instead would delay the scan by a turn and let several
    /// callbacks race past the `hasScanned` guard, each burning a fresh
    /// single-use code.
    nonisolated func metadataOutput(
        _ output: AVCaptureMetadataOutput,
        didOutput objects: [AVMetadataObject],
        from connection: AVCaptureConnection
    ) {
        guard let object = objects.first as? AVMetadataMachineReadableCodeObject,
              let value = object.stringValue
        else { return }
        MainActor.assumeIsolated {
            // One scan only. A QR stays in frame for many callbacks.
            guard !hasScanned else { return }
            hasScanned = true
            onScan?(value)
        }
    }
}
