#if DEBUG
import Foundation
import SwiftUI

/// An intentionally hidden device-only check for the exact ATS boundary used
/// by StoreRelease. Set `DROVER_SHOW_ATS_PROBE=1` in a Debug scheme and build
/// with `DROVER_ATS_MODE=local`; it is not compiled into Release artifacts.
struct ATSProbeView: View {
    private struct Result: Identifiable {
        let id = UUID()
        let name: String
        let passed: Bool
        let detail: String
    }

    @State private var tailscaleIP = ""
    @State private var lanIP = ""
    @State private var magicDNSName = ""
    @State private var port = "7080"
    @State private var results: [Result] = []
    @State private var running = false

    var body: some View {
        NavigationStack {
            Form {
                Section("Targets") {
                    TextField("Tailscale IP (100.x.y.z)", text: $tailscaleIP)
                        .keyboardType(.numbersAndPunctuation)
                    TextField("LAN IP (192.168.x.y)", text: $lanIP)
                        .keyboardType(.numbersAndPunctuation)
                    TextField("MagicDNS FQDN (name.ts.net)", text: $magicDNSName)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    TextField("Port", text: $port)
                        .keyboardType(.numberPad)
                }

                Section {
                    Button(running ? "Probing…" : "Run ATS probe") {
                        Task { await run() }
                    }
                    .disabled(running || tailscaleIP.isEmpty || lanIP.isEmpty || magicDNSName.isEmpty)
                } footer: {
                    Text("HTTP checks /healthz. The WebSocket check deliberately uses that same endpoint: a non-ATS handshake error is a pass because this probe only verifies that ws:// reaches URLSession transport policy.")
                }

                if !results.isEmpty {
                    Section("Results") {
                        ForEach(results) { result in
                            VStack(alignment: .leading, spacing: 3) {
                                Label(result.name, systemImage: result.passed ? "checkmark.circle.fill" : "xmark.circle.fill")
                                    .foregroundStyle(result.passed ? .green : .red)
                                Text(result.detail)
                                    .font(.footnote)
                                    .foregroundStyle(.secondary)
                            }
                        }
                    }
                }
            }
            .navigationTitle("ATS probe")
        }
    }

    @MainActor
    private func run() async {
        running = true
        results = []
        let trimmedPort = port.trimmingCharacters(in: .whitespacesAndNewlines)
        let tsHost = tailscaleIP.trimmingCharacters(in: .whitespacesAndNewlines)
        let lanHost = lanIP.trimmingCharacters(in: .whitespacesAndNewlines)
        let dnsHost = magicDNSName.trimmingCharacters(in: .whitespacesAndNewlines)
        let authority = { (host: String) in "\(host):\(trimmedPort)" }

        results.append(await httpResult("HTTP to Tailscale IP", authority(tsHost)))
        results.append(await webSocketResult("WS to Tailscale IP", authority(tsHost)))
        results.append(await httpResult("HTTP to LAN IP", authority(lanHost)))
        results.append(await httpResult("HTTP to MagicDNS FQDN", authority(dnsHost)))
        running = false
    }

    private func httpResult(_ name: String, _ authority: String) async -> Result {
        guard let url = URL(string: "http://\(authority)/healthz") else {
            return Result(name: name, passed: false, detail: "Invalid URL")
        }
        var request = URLRequest(url: url)
        request.timeoutInterval = 8
        do {
            let (_, response) = try await URLSession.shared.data(for: request)
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            return Result(name: name, passed: true, detail: "HTTP response \(status)")
        } catch {
            return Result(name: name, passed: false, detail: describe(error))
        }
    }

    private func webSocketResult(_ name: String, _ authority: String) async -> Result {
        guard let url = URL(string: "ws://\(authority)/healthz") else {
            return Result(name: name, passed: false, detail: "Invalid URL")
        }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 8
        let session = URLSession(configuration: configuration)
        let task = session.webSocketTask(with: url)
        task.resume()
        do {
            _ = try await task.receive()
            task.cancel(with: .goingAway, reason: nil)
            return Result(name: name, passed: true, detail: "WebSocket accepted")
        } catch {
            task.cancel(with: .goingAway, reason: nil)
            // The hub's HTTP health endpoint is not a WebSocket route. A
            // handshake rejection still proves `ws://` made it through ATS;
            // -1022 is the one result this probe is trying to detect.
            let atsBlocked = (error as? URLError)?.code == .appTransportSecurityRequiresSecureConnection
            return Result(
                name: name,
                passed: !atsBlocked,
                detail: atsBlocked ? describe(error) : "ATS allowed; endpoint rejected WebSocket (\(describe(error)))"
            )
        }
    }

    private func describe(_ error: Error) -> String {
        let nsError = error as NSError
        return "\(nsError.domain) \(nsError.code): \(nsError.localizedDescription)"
    }
}
#endif
