import SwiftUI
import DroverKit

enum OnboardingStep: Int, CaseIterable, Identifiable, Sendable {
    case welcome = 0
    case serverSetup = 1
    case pair = 2

    var id: Int { rawValue }

    var title: String {
        switch self {
        case .welcome: return "Welcome"
        case .serverSetup: return "Server Setup"
        case .pair: return "Pair & Connect"
        }
    }
}

/// The multi-step first-launch onboarding experience for Drover.
/// Guides the user through fleet overview, server setup (Hub vs Host), and instant QR / manual pairing.
struct OnboardingView: View {
    var environment: AppEnvironment
    var onFinished: () -> Void = {}

    @State private var step: OnboardingStep = .welcome
    @State private var selectedSetupMode: ServerSetupMode = .hub
    @StateObject private var pairingModel = PairingModel()

    var body: some View {
        VStack(spacing: 0) {
            topNavigationHeader

            TabView(selection: $step) {
                welcomeStep
                    .tag(OnboardingStep.welcome)

                serverSetupStep
                    .tag(OnboardingStep.serverSetup)

                pairStep
                    .tag(OnboardingStep.pair)
            }
            .tabViewStyle(.page(indexDisplayMode: .never))
            .animation(.easeInOut(duration: 0.25), value: step)
        }
        .background(DroverColor.bg)
        .navigationBarHidden(true)
    }

    // MARK: - Navigation Header

    private var topNavigationHeader: some View {
        HStack {
            if step != .welcome {
                Button {
                    withAnimation(.easeInOut(duration: 0.25)) {
                        goBack()
                    }
                } label: {
                    HStack(spacing: 4) {
                        Image(systemName: "chevron.left")
                            .font(.system(size: 14, weight: .semibold))
                        Text("Back")
                            .font(.system(.subheadline, design: .default))
                    }
                    .foregroundStyle(DroverColor.accentHi)
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("onboarding-back-button")
            } else {
                Spacer().frame(width: 60)
            }

            Spacer()

            HStack(spacing: 6) {
                ForEach(OnboardingStep.allCases) { s in
                    Capsule()
                        .fill(s == step ? AnyShapeStyle(DroverColor.accentHi) : AnyShapeStyle(DroverColor.line))
                        .frame(width: s == step ? 18 : 6, height: 6)
                }
            }
            .accessibilityIdentifier("onboarding-progress-dots")

            Spacer()

            Spacer().frame(width: 60)
        }
        .padding(.horizontal, 18)
        .padding(.top, 12)
        .padding(.bottom, 8)
    }

    private func goBack() {
        switch step {
        case .welcome:
            break
        case .serverSetup:
            step = .welcome
        case .pair:
            step = .serverSetup
        }
    }

    // MARK: - Step 1: Welcome

    private var welcomeStep: some View {
        ScrollView {
            VStack(spacing: 24) {
                Spacer(minLength: 8)

                Image("DroverHero")
                    .resizable()
                    .aspectRatio(contentMode: .fit)
                    .frame(maxWidth: 340, maxHeight: 240)
                    .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
                    .overlay {
                        RoundedRectangle(cornerRadius: 16, style: .continuous)
                            .strokeBorder(DroverColor.line, lineWidth: 1)
                    }
                    .accessibilityIdentifier("onboarding-hero-image")

                VStack(spacing: 12) {
                    Text("Fleet Control for Coding Agents")
                        .droverText(.h1)
                        .multilineTextAlignment(.center)
                        .foregroundStyle(DroverColor.text)
                        .accessibilityIdentifier("onboarding-welcome-headline")

                    Text("Supervise, inspect, and command Claude Code, Codex, and Agy sessions across your machines from your phone.")
                        .droverText(.body)
                        .foregroundStyle(DroverColor.muted)
                        .multilineTextAlignment(.center)
                        .padding(.horizontal, 8)
                        .fixedSize(horizontal: false, vertical: true)
                        .accessibilityIdentifier("onboarding-welcome-subtitle")
                }

                Spacer(minLength: 16)

                VStack(spacing: 12) {
                    Button {
                        withAnimation(.easeInOut(duration: 0.25)) {
                            step = .serverSetup
                        }
                    } label: {
                        Text("Set Up Drover")
                            .font(.system(.subheadline, design: .default, weight: .medium))
                            .foregroundStyle(DroverColor.accentHi)
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 13)
                            .background(DroverColor.bg, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                            .overlay {
                                RoundedRectangle(cornerRadius: 10, style: .continuous)
                                    .strokeBorder(DroverColor.accent, lineWidth: 1)
                            }
                    }
                    .buttonStyle(.plain)
                    .accessibilityIdentifier("onboarding-setup-drover-button")

                    Button {
                        withAnimation(.easeInOut(duration: 0.25)) {
                            step = .pair
                        }
                    } label: {
                        Text("I already have a server")
                            .font(.system(.subheadline, design: .default, weight: .medium))
                            .foregroundStyle(DroverColor.faint)
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 13)
                    }
                    .buttonStyle(.plain)
                    .accessibilityIdentifier("onboarding-already-have-server-button")
                }
            }
            .padding(.horizontal, 20)
            .padding(.bottom, 28)
        }
    }

    // MARK: - Step 2: Server Setup

    private var serverSetupStep: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                ServerSetupContent(selectedMode: $selectedSetupMode)

                Button {
                    withAnimation(.easeInOut(duration: 0.25)) {
                        step = .pair
                    }
                } label: {
                    Text("Next: Pair Device")
                        .font(.system(.subheadline, design: .default, weight: .medium))
                        .foregroundStyle(DroverColor.accentHi)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 13)
                        .background(DroverColor.bg, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                        .overlay {
                            RoundedRectangle(cornerRadius: 10, style: .continuous)
                                .strokeBorder(DroverColor.accent, lineWidth: 1)
                        }
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("onboarding-next-pair-button")
            }
            .padding(.horizontal, 18)
            .padding(.top, 10)
            .padding(.bottom, 28)
        }
    }

    // MARK: - Step 3: Pair & Connect

    private var pairStep: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                if step == .pair {
                    QRScannerView { scanned in
                        guard let payload = PairingPayload(scanned: scanned) else { return }
                        Task { await pair(payload) }
                    }
                    .frame(height: 240)
                    .frame(maxWidth: .infinity)
                    .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
                    .overlay {
                        RoundedRectangle(cornerRadius: 14, style: .continuous)
                            .strokeBorder(DroverColor.line, lineWidth: 1)
                    }
                    .accessibilityIdentifier("onboarding-qr-scanner")
                } else {
                    RoundedRectangle(cornerRadius: 14, style: .continuous)
                        .fill(Color.black)
                        .frame(height: 240)
                        .frame(maxWidth: .infinity)
                }

                Text("Run `drover-server pair` on your Drover machine, then scan the code it prints.")
                    .droverText(.subtitle)
                    .foregroundStyle(DroverColor.faint)
                    .fixedSize(horizontal: false, vertical: true)

                Divider().overlay(DroverColor.line)

                Text("Or enter it by hand").droverText(.h3)

                field("http://host:7080", text: $pairingModel.serverURLString)
                    .keyboardType(.URL)
                    .accessibilityIdentifier("onboarding-server-url-field")
                field("K7QP-2M4X", text: $pairingModel.manualCode)
                    .textInputAutocapitalization(.characters)
                    .accessibilityIdentifier("onboarding-pairing-code-field")

                Button {
                    guard let payload = pairingModel.manualPayload() else { return }
                    Task { await pair(payload) }
                } label: {
                    Text(pairingModel.isPairing ? "Pairing..." : "Pair")
                        .droverText(.h3)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 12)
                }
                .disabled(!pairingModel.canSubmitManualCode || pairingModel.isPairing)
                .foregroundStyle(DroverColor.accentHi)
                .overlay {
                    RoundedRectangle(cornerRadius: 10, style: .continuous)
                        .strokeBorder(DroverColor.accentHi, lineWidth: 1)
                }
                .accessibilityIdentifier("onboarding-pair-submit-button")

                if let statusMessage = pairingModel.statusMessage {
                    statusRow(statusMessage)
                }
            }
            .padding(.horizontal, 18)
            .padding(.top, 10)
            .padding(.bottom, 28)
        }
        .scrollDismissesKeyboard(.interactively)
        .onAppear {
            if pairingModel.serverURLString.isEmpty, let config = environment.config {
                pairingModel.serverURLString = config.baseURL.absoluteString
            }
        }
    }

    // MARK: - Helpers

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

    private func statusRow(_ message: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 9) {
            Image(systemName: pairingModel.statusIsError
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
        .accessibilityIdentifier(pairingModel.statusIsError ? "onboarding-pair-error" : "onboarding-pair-success")
    }

    private func pair(_ payload: PairingPayload) async {
        guard !pairingModel.isPairing else { return }
        pairingModel.isPairing = true
        pairingModel.statusMessage = "Pairing..."
        pairingModel.statusIsError = false
        defer { pairingModel.isPairing = false }

        let response: PairResponse
        do {
            response = try await DroverClient.pair(
                payload: payload,
                deviceName: UITestOverrides.pairingDeviceName(
                    fallback: UIDevice.current.name
                )
            )
        } catch {
            pairingModel.statusMessage = error.localizedDescription
            pairingModel.statusIsError = true
            return
        }

        switch await environment.configure(
            urlString: payload.serverURL.absoluteString,
            token: response.token
        ) {
        case .success:
            pairingModel.statusMessage = "Paired with \(response.fleetName)."
            pairingModel.statusIsError = false
            onFinished()
        case .failure(let detail):
            pairingModel.statusMessage = detail
            pairingModel.statusIsError = true
        }
    }
}
