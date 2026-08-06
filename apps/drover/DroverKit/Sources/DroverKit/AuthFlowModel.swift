import Foundation
import Observation

/// Observable state for a harness-managed authentication flow. The model
/// only exposes the provider-supplied flow details needed by the UI; it never
/// handles credentials or provider tokens directly.
@MainActor
@Observable
public final class AuthFlowModel {
    private let client: DroverClient
    public let hostID: String
    public let harness: String

    // `nonisolated(unsafe)` lets `deinit` cancel the task under Swift 6;
    // every other access is isolated to the main actor.
    private nonisolated(unsafe) var pollTask: Task<Void, Never>?
    private var pollGeneration = 0

    public var status: HarnessAuthStatus?
    public var flow: HarnessAuthFlow?
    public var isStarting = false
    public var errorMessage: String?

    public init(client: DroverClient, hostID: String, harness: String) {
        self.client = client
        self.hostID = hostID
        self.harness = harness
    }

    deinit {
        pollTask?.cancel()
    }

    public func refreshStatus() async {
        do {
            status = try await client.authStatus(hostID: hostID, harness: harness)
            errorMessage = nil
        } catch {
            errorMessage = Self.errorMessage(for: error)
        }
    }

    public func start() async {
        isStarting = true
        defer { isStarting = false }

        do {
            flow = try await client.startAuthFlow(hostID: hostID, harness: harness)
            errorMessage = nil
            startPolling()
        } catch {
            errorMessage = Self.errorMessage(for: error)
        }
    }

    public func cancel() async {
        guard let flow else { return }
        stopPolling()

        do {
            self.flow = try await client.cancelAuthFlow(
                hostID: hostID, harness: harness, flowID: flow.flowID)
            errorMessage = nil
        } catch {
            errorMessage = Self.errorMessage(for: error)
            if self.flow?.isTerminal == false {
                startPolling()
            }
        }
    }

    public func startPolling(every seconds: Double = 1.5) {
        stopPolling()
        let generation = pollGeneration
        pollTask = Task { [weak self] in
            var transientFailureCount = 0
            while !Task.isCancelled {
                do {
                    guard let self, self.pollGeneration == generation,
                          let flow = self.flow, !flow.isTerminal else { return }
                    let fresh = try await self.client.authFlow(
                        hostID: self.hostID, harness: self.harness, flowID: flow.flowID)
                    guard !Task.isCancelled, self.pollGeneration == generation else { return }
                    self.flow = fresh
                    self.errorMessage = nil
                    transientFailureCount = 0
                    if fresh.isTerminal { return }
                } catch {
                    guard !Task.isCancelled, let self, self.pollGeneration == generation else { return }
                    self.errorMessage = Self.errorMessage(for: error)
                    guard Self.isRetryablePollingError(error) else { return }
                    let multiplier = pow(2.0, Double(min(transientFailureCount, 10)))
                    let delay = min(max(seconds, 0.01) * multiplier, 10)
                    transientFailureCount += 1
                    try? await Task.sleep(for: .seconds(delay))
                    continue
                }

                guard !Task.isCancelled, self?.pollGeneration == generation else { return }
                try? await Task.sleep(for: .seconds(seconds))
            }
        }
    }

    public func stopPolling() {
        pollGeneration &+= 1
        pollTask?.cancel()
        pollTask = nil
    }

    private static func errorMessage(for error: Error) -> String {
        switch error {
        case DroverError.badRequest(let message), DroverError.conflict(let message),
             DroverError.unavailable(let message):
            return message
        case DroverError.httpStatus(_, let message):
            return message
        case DroverError.unauthorized:
            return "token rejected - check Settings"
        default:
            return "\(error)"
        }
    }

    private static func isRetryablePollingError(_ error: Error) -> Bool {
        if case DroverError.transport = error {
            return true
        }
        if case DroverError.httpStatus(let status, _) = error {
            return status == 429 || (500..<600).contains(status)
        }
        return false
    }
}
