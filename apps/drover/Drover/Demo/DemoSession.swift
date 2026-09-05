import Foundation
import SwiftUI
import DroverKit

/// Release-safe data shared with the deterministic Q3 fixture. Transport
/// selectors, fault controls, and receipt mutation remain in DEBUG-only
/// files; this is the shipping demo's fixed, local-only presentation data.
enum DemoScenarioData {
    static let serverURLString = "https://demo.drover.invalid"
    static let hostID = FixtureScenarioData.coreJourney.hostID
    static let credentialBindingID = FixtureScenarioData.coreJourney.credentialBindingID
    static let hostName = "Sample Studio Mac"
    static let approvalSessionID = "demo-approval-session"
    static let chatSessionID = "demo-chat-session"
    static let launchedSessionID = "demo-launched-session"
    static let approvalRequestID = "demo-approval-request"
    static let syntheticToken = "demo-local-token-not-a-credential"
}

/// Thread-safe state shared by lifecycle boundaries that can be reached from
/// a background callback. It is deliberately process-local: entering the
/// demo does not write a preference that survives an app relaunch.
final class DemoActivityGate: @unchecked Sendable {
    static let shared = DemoActivityGate()

    private let lock = NSLock()
    private var active = false

    var isActive: Bool { lock.withLock { active } }

    func activate() { lock.withLock { active = true } }
    func deactivate() { lock.withLock { active = false } }
}

/// Retains only the operations that crossed a demo boundary. The production
/// code never records content or credentials; tests use this to prove that a
/// demo client did not escape to a live HTTP, WebSocket, or APNs operation.
struct DemoOperationSnapshot: Sendable, Equatable {
    var localHTTPRequests = 0
    var localWebSocketConnections = 0
    var liveHTTPRequests = 0
    var liveWebSocketConnections = 0
    var apnsRegistrationRequests = 0
}

final class DemoOperationRecorder: @unchecked Sendable {
    private let lock = NSLock()
    private var value = DemoOperationSnapshot()

    var snapshot: DemoOperationSnapshot { lock.withLock { value } }

    func recordLocalHTTP() { lock.withLock { value.localHTTPRequests += 1 } }
    func recordLocalWebSocket() { lock.withLock { value.localWebSocketConnections += 1 } }
    func recordAPNsRegistration() { lock.withLock { value.apnsRegistrationRequests += 1 } }
}

/// Bounded, in-memory recovery for the demo. It deliberately has no file
/// root, defaults suite, Keychain binding index, or durable lifetime.
private actor DemoRecoveryStore: ChatRecoveryPersisting {
    private static let maximumRecords = 4
    private static let maximumBytes = 64 * 1024
    private var snapshots: [ChatRecoveryKey: ChatRecoverySnapshot] = [:]

    func load(for key: ChatRecoveryKey) async throws -> ChatRecoverySnapshot? {
        snapshots[key]
    }

    func save(_ snapshot: ChatRecoverySnapshot, for key: ChatRecoveryKey) async throws {
        let bytes = snapshot.draftText.lengthOfBytes(using: .utf8)
            + snapshot.draftAttachments.reduce(0) { $0 + $1.data.count }
            + (snapshot.deferredTurn?.text.lengthOfBytes(using: .utf8) ?? 0)
            + (snapshot.deferredTurn?.attachments.reduce(0) { $0 + $1.data.count } ?? 0)
            + (snapshot.pendingTurn?.text.lengthOfBytes(using: .utf8) ?? 0)
            + (snapshot.pendingTurn?.attachments.reduce(0) { $0 + $1.data.count } ?? 0)
        guard bytes <= Self.maximumBytes else { throw ChatRecoveryError.quotaExceeded }
        guard snapshots[key] != nil || snapshots.count < Self.maximumRecords else {
            throw ChatRecoveryError.quotaExceeded
        }
        snapshots[key] = snapshot
    }

    func remove(for key: ChatRecoveryKey) async throws {
        snapshots.removeValue(forKey: key)
    }

    func purge(bindingID: UUID) async throws {
        snapshots = snapshots.filter { $0.key.credentialBindingID != bindingID }
    }

    func sweep(keeping bindingIDs: Set<UUID>) async throws {
        snapshots = snapshots.filter { bindingIDs.contains($0.key.credentialBindingID) }
    }

    func eraseAllAfterCredentialDeletion() async throws {
        snapshots.removeAll()
    }

    func reset() {
        snapshots.removeAll()
    }
}

struct DemoNotifier: Notifying {
    func notify(title: String, body: String, id: String) async {}
    func setBadge(_ count: Int) async {}
}

/// Owns one evaluation visit. It creates the isolated AppEnvironment before
/// any shared view is built, so the saved client, Keychain and recovery root
/// are never touched by a demo SessionStore or ChatModel.
@MainActor
final class DemoSession {
    let transport: DemoTransport
    let environment: AppEnvironment
    let defaults: UserDefaults
    private let recoveryStore: DemoRecoveryStore
    private let catalogStore: HarnessModelCatalogStore
    private let defaultsSuiteName: String

    init(recorder: DemoOperationRecorder = DemoOperationRecorder()) throws {
        let id = UUID().uuidString
        let suiteName = "com.arnab.drover.evaluation-demo.\(id)"
        guard let defaults = UserDefaults(suiteName: suiteName) else {
            throw DemoSessionError.defaultsUnavailable
        }
        let transport = try DemoTransport(recorder: recorder)
        let recoveryStore = DemoRecoveryStore()
        self.transport = transport
        self.defaults = defaults
        self.recoveryStore = recoveryStore
        self.catalogStore = HarnessModelCatalogStore(defaults: defaults)
        self.defaultsSuiteName = suiteName
        self.environment = AppEnvironment(
            isolatedClient: transport.client,
            defaults: defaults,
            tokenStore: TokenStore(service: "com.arnab.drover.evaluation-demo.\(id)"),
            recoveryStore: recoveryStore
        )
    }

    var chatModelFactory: ChatModelFactory {
        { [transport, catalogStore, recoveryStore, environment] client, sessionID, harness in
            ChatModel(
                client: client,
                sessionID: sessionID,
                harness: harness,
                store: catalogStore,
                deliveryConfirmationTimeout: .milliseconds(200),
                recoveryStore: recoveryStore,
                recoveryWriteGate: environment.chatRecoveryWriteGate,
                recoveryGeneration: environment.chatRecoveryGeneration,
                streamFactory: { client, sessionID in
                    MessageStream(
                        client: client,
                        sessionID: sessionID,
                        connector: transport.webSocketConnector,
                        reconnectBaseDelay: .milliseconds(350)
                    )
                }
            )
        }
    }

    func simulateReconnect() {
        transport.simulateReconnect()
    }

    func end() {
        transport.reset()
        defaults.removePersistentDomain(forName: defaultsSuiteName)
        Task { await recoveryStore.reset() }
    }
}

private enum DemoSessionError: LocalizedError {
    case defaultsUnavailable

    var errorDescription: String? {
        "Could not start the local evaluation demo."
    }
}

/// Settings while demo mode is active intentionally exposes only local demo
/// controls and the public help links. Pairing, saved-token changes, content
/// configuration, and sign-out all remain unavailable until the demo exits.
struct DemoSettingsView: View {
    let session: DemoSession
    let onReset: () -> Void
    let onExit: () -> Void
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                VStack(alignment: .leading, spacing: 8) {
                    Label("Evaluation Demo", systemImage: "play.circle.fill")
                        .droverText(.h2)
                    Text("All sample sessions, approvals, and reconnects run locally on this device. No server, account, or saved connection is used.")
                        .droverText(.body)
                        .foregroundStyle(DroverColor.muted)
                }

                Button(action: onReset) {
                    Text("Reset Demo")
                        .font(.system(.subheadline, design: .default, weight: .medium))
                        .foregroundStyle(DroverColor.accentHi)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 13)
                        .overlay {
                            RoundedRectangle(cornerRadius: 10, style: .continuous)
                                .strokeBorder(DroverColor.accent, lineWidth: 1)
                        }
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("demo-settings-reset")

                Button("Show reconnecting state") {
                    session.simulateReconnect()
                    dismiss()
                }
                .accessibilityIdentifier("demo-settings-reconnect")

                VStack(alignment: .leading, spacing: 8) {
                    Text("Help").droverText(.h3)
                    Link("Privacy", destination: Self.privacyURL)
                        .accessibilityIdentifier("demo-settings-privacy-link")
                    Link("Support", destination: Self.supportURL)
                        .accessibilityIdentifier("demo-settings-support-link")
                }
                .foregroundStyle(DroverColor.accentHi)

                Button(role: .destructive) {
                    onExit()
                } label: {
                    Text("Exit Demo")
                        .font(.system(.subheadline, design: .default, weight: .medium))
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 13)
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("demo-settings-exit")
            }
            .padding(.horizontal, 18)
            .padding(.vertical, 18)
        }
        .background(DroverColor.bg)
        .navigationTitle("Demo Settings")
        .navigationBarTitleDisplayMode(.inline)
    }

    private static let privacyURL = URL(
        string: "https://github.com/arniesaha/drover/blob/main/docs/privacy.md"
    )!
    private static let supportURL = URL(
        string: "https://github.com/arniesaha/drover/blob/main/docs/support.md"
    )!
}
