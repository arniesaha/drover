import Foundation

// Capability schema v1 (#418) as the iOS client reads it (#420).
//
// Every harness control in the app is derived from these types, never from a
// harness name. The rules mirror docs/harness-adapter-architecture.md:
// - a control is offered only when the host's matrix advertises it;
// - a host or row without a matrix is legacy metadata and advertises nothing;
// - an unsupported schema version or a malformed matrix advertises nothing;
// - unknown additive fields are ignored.

/// A session mode a harness can be launched in.
public enum HarnessLaunchMode: String, Sendable, Hashable, CaseIterable {
    case structured
    case pty
}

/// One harness's v1 capability matrix. Booleans missing on the wire are false
/// and missing attachments are empty, as the schema specifies.
public struct HarnessCapabilities: Sendable, Equatable, Hashable {
    public static let supportedSchemaVersion = 1

    public var launchModes: [HarnessLaunchMode]
    public var approvals: Bool
    public var interrupt: Bool
    public var nativeResume: Bool
    public var modelCatalog: Bool
    public var usage: Bool
    public var worktree: Bool
    public var interactiveAuth: Bool
    /// Model and effort overrides reach later turns of a running session.
    /// Additive v1 field: hosts that predate it omit it, which reads as false.
    public var turnPreferences: Bool
    /// Accepted attachment MIME types; `type/*` wildcards are honoured.
    public var attachments: [String]

    public init(
        launchModes: [HarnessLaunchMode],
        approvals: Bool = false,
        interrupt: Bool = false,
        nativeResume: Bool = false,
        modelCatalog: Bool = false,
        usage: Bool = false,
        worktree: Bool = false,
        interactiveAuth: Bool = false,
        turnPreferences: Bool = false,
        attachments: [String] = []
    ) {
        self.launchModes = launchModes
        self.approvals = approvals
        self.interrupt = interrupt
        self.nativeResume = nativeResume
        self.modelCatalog = modelCatalog
        self.usage = usage
        self.worktree = worktree
        self.interactiveAuth = interactiveAuth
        self.turnPreferences = turnPreferences
        self.attachments = attachments
    }

    /// Advertises no operation at all.
    public static let none = HarnessCapabilities(launchModes: [])

    /// Structured wins when both are advertised: only a structured session
    /// carries a starting prompt, attachments, approvals and run preferences.
    /// PTY is chosen only when it is the sole advertised mode.
    public var preferredLaunchMode: HarnessLaunchMode? {
        if launchModes.contains(.structured) { return .structured }
        if launchModes.contains(.pty) { return .pty }
        return nil
    }

    public func accepts(mediaType: String) -> Bool {
        let wanted = mediaType.lowercased()
        let family = wanted.split(separator: "/").first.map(String.init) ?? wanted
        return attachments.contains { accepted in
            let accepted = accepted.lowercased()
            return accepted == wanted || accepted == "\(family)/*"
        }
    }

    /// Parses a v1 matrix object. Nil when it is malformed, which callers
    /// treat as advertising nothing.
    init?(v1 object: [String: JSONValue]) {
        guard case .array(let rawModes)? = object["launch_modes"] else { return nil }
        var modes: [HarnessLaunchMode] = []
        for raw in rawModes {
            guard let name = raw.stringValue else { return nil }
            // A mode this client does not know cannot be launched by it.
            if let mode = HarnessLaunchMode(rawValue: name), !modes.contains(mode) {
                modes.append(mode)
            }
        }
        func flag(_ key: String) -> Bool? {
            switch object[key] {
            case nil: return false
            case .bool(let value)?: return value
            default: return nil
            }
        }
        guard let approvals = flag("approvals"),
              let interrupt = flag("interrupt"),
              let nativeResume = flag("native_resume"),
              let modelCatalog = flag("model_catalog"),
              let usage = flag("usage"),
              let worktree = flag("worktree"),
              let interactiveAuth = flag("interactive_auth"),
              let turnPreferences = flag("turn_preferences")
        else { return nil }
        var attachments: [String] = []
        switch object["attachments"] {
        case nil: break
        case .array(let values)?:
            for value in values {
                guard let mime = value.stringValue,
                      mime.range(of: #"^[a-z0-9.+-]+/(?:[a-z0-9.+-]+|\*)$"#, options: .regularExpression) != nil
                else { return nil }
                attachments.append(mime)
            }
        default: return nil
        }
        self.init(
            launchModes: modes,
            approvals: approvals,
            interrupt: interrupt,
            nativeResume: nativeResume,
            modelCatalog: modelCatalog,
            usage: usage,
            worktree: worktree,
            interactiveAuth: interactiveAuth,
            turnPreferences: turnPreferences && modelCatalog,
            attachments: attachments
        )
    }
}

/// What a host said about one harness's capabilities.
public enum HarnessCapabilityAdvertisement: Sendable, Equatable, Hashable {
    /// A supported v1 matrix.
    case v1(HarnessCapabilities)
    /// No matrix: a pre-#418 host. Metadata only.
    case legacy
    /// A newer schema this app cannot interpret.
    case unsupportedSchema(Int)
    /// A present but null or malformed matrix. Never a legacy fallback.
    case invalid
}

/// One harness row from a host's capability envelope.
public struct HarnessOffer: Sendable, Equatable, Hashable, Identifiable {
    public var name: String
    /// The host's availability gate. Nil for legacy string-only rows, which
    /// carry no flag at all.
    public var enabled: Bool?
    public var advertisement: HarnessCapabilityAdvertisement

    public var id: String { name }

    public init(name: String, enabled: Bool?, advertisement: HarnessCapabilityAdvertisement) {
        self.name = name
        self.enabled = enabled
        self.advertisement = advertisement
    }

    public init(name: String, enabled: Bool = true, capabilities: HarnessCapabilities) {
        self.init(name: name, enabled: enabled, advertisement: .v1(capabilities))
    }

    /// The advertised matrix, or nothing for anything but a supported v1 row.
    public var capabilities: HarnessCapabilities {
        if case .v1(let capabilities) = advertisement { return capabilities }
        return .none
    }

    /// Launch requires the host's enabled flag, a supported schema and an
    /// explicitly advertised mode.
    public var launchMode: HarnessLaunchMode? {
        guard enabled == true else { return nil }
        return capabilities.preferredLaunchMode
    }

    public var isLaunchable: Bool { launchMode != nil }

    /// Why this row cannot be launched from iOS, or nil when it can — or when
    /// the host simply has it switched off, which needs no explanation.
    public var unavailableReason: String? {
        switch advertisement {
        case .legacy:
            return enabled == false ? nil : HarnessCapabilityCopy.legacyHost
        case .unsupportedSchema(let version):
            return HarnessCapabilityCopy.unsupportedSchema(version)
        case .invalid:
            return HarnessCapabilityCopy.invalidMatrix
        case .v1:
            return nil
        }
    }

    /// Parses one untrusted envelope row. Strings are legacy names; objects
    /// need a nonempty `name`. Anything else is dropped.
    init?(row: JSONValue) {
        switch row {
        case .string(let name):
            guard !name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return nil }
            self.init(name: name, enabled: nil, advertisement: .legacy)
        case .object(let object):
            guard let name = object["name"]?.stringValue,
                  !name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            else { return nil }
            let enabled = object["enabled"]?.boolValue ?? false
            self.init(name: name, enabled: enabled,
                      advertisement: Self.advertisement(object["capabilities"], name: name))
        default:
            return nil
        }
    }

    private static func advertisement(_ raw: JSONValue?, name: String) -> HarnessCapabilityAdvertisement {
        guard let raw else { return .legacy }
        guard case .object(let matrix) = raw,
              let version = matrix["schema_version"]?.numberValue,
              version >= 1, version.rounded() == version, version <= Double(Int32.max)
        else { return .invalid }
        if let identity = matrix["harness_id"], identity.stringValue != name {
            return .invalid
        }
        guard Int(version) == HarnessCapabilities.supportedSchemaVersion else {
            return .unsupportedSchema(Int(version))
        }
        guard let capabilities = HarnessCapabilities(v1: matrix) else { return .invalid }
        return .v1(capabilities)
    }
}

/// User-facing explanations for controls the capability envelope withholds.
/// Kept in one place so the launch sheet, chat and VoiceOver say the same thing.
public enum HarnessCapabilityCopy {
    public static let legacyHost =
        "This host runs an older Drover that doesn't advertise harness capabilities. Update Drover on the host to launch or control sessions from iOS."
    public static func unsupportedSchema(_ version: Int) -> String {
        "This host advertises capability schema v\(version), which this app doesn't understand. Update the Drover app."
    }
    public static let invalidMatrix =
        "This host sent an invalid capability declaration for this harness."
    public static let noLaunchableHarness = "No harness on this host can be launched."
    public static let missingFromSnapshot =
        "This session's host or harness isn't in the current fleet snapshot."
    public static let checking = "Checking what this harness supports…"
    public static let interruptUnsupported = "This harness doesn't support interrupt."
    public static let approvalsUnsupported =
        "This harness doesn't advertise approvals, so this request can't be answered from iOS. Answer it on the host."
    public static let attachmentsUnsupported = "This harness doesn't accept image attachments."
    public static let preferencesLocked =
        "Model and effort are fixed for this session. Start a new session to change them."
    public static let worktreeIsolation = "This harness can run in an isolated Drover worktree."
}

/// Which session controls one harness on one host may show, derived only from
/// its advertised capabilities. Pure, so every rule is unit-testable.
public struct HarnessControls: Sendable, Equatable {
    /// False until the snapshot naming this session's host has been read.
    public let isResolved: Bool
    /// The matching row, or nil when the host or harness is not listed.
    public let offer: HarnessOffer?

    /// Nothing is known yet: every control is withheld.
    public static let unresolved = HarnessControls(isResolved: false, offer: nil)

    public init(offer: HarnessOffer?) {
        self.init(isResolved: true, offer: offer)
    }

    /// Resolves `harness` on `hostID` against a fleet snapshot.
    public init(snapshot: HarnessSnapshot, hostID: String, harness: String) {
        let host = snapshot.hosts.first { $0.id == hostID }
        self.init(offer: host?.offer(named: harness))
    }

    private init(isResolved: Bool, offer: HarnessOffer?) {
        self.isResolved = isResolved
        self.offer = offer
    }

    public var capabilities: HarnessCapabilities { offer?.capabilities ?? .none }

    public var launchMode: HarnessLaunchMode? { offer?.launchMode }
    public var showsApprovals: Bool { capabilities.approvals }
    public var canInterrupt: Bool { capabilities.interrupt }
    public var supportsNativeResume: Bool { capabilities.nativeResume }
    public var showsModelControls: Bool { capabilities.modelCatalog }
    public var canChangePreferencesMidSession: Bool {
        capabilities.modelCatalog && capabilities.turnPreferences
    }
    public var offersSignIn: Bool { capabilities.interactiveAuth }
    public var explainsWorktreeIsolation: Bool { capabilities.worktree }
    /// The app only produces JPEG attachments (see `ImageDownscaler`).
    public var acceptsImageAttachments: Bool { capabilities.accepts(mediaType: "image/jpeg") }

    /// Why a withheld control is withheld: unresolved, host-level, or the
    /// control-specific fallback for a v1 matrix that simply lacks it.
    private func reason(unless supported: Bool, otherwise specific: String) -> String? {
        guard !supported else { return nil }
        guard isResolved else { return HarnessCapabilityCopy.checking }
        guard let offer else { return HarnessCapabilityCopy.missingFromSnapshot }
        return offer.unavailableReason ?? specific
    }

    public var interruptUnavailableReason: String? {
        reason(unless: canInterrupt, otherwise: HarnessCapabilityCopy.interruptUnsupported)
    }

    public var approvalsUnavailableReason: String? {
        reason(unless: showsApprovals, otherwise: HarnessCapabilityCopy.approvalsUnsupported)
    }

    public var attachmentsUnavailableReason: String? {
        reason(unless: acceptsImageAttachments, otherwise: HarnessCapabilityCopy.attachmentsUnsupported)
    }

    public var preferencesLockedReason: String? {
        reason(unless: canChangePreferencesMidSession, otherwise: HarnessCapabilityCopy.preferencesLocked)
    }
}

extension HostSummary {
    public func offer(named harness: String) -> HarnessOffer? {
        harnessOffers.first { $0.name == harness }
    }

    /// Rows the launch sheet may offer, structured-capable ones first, each
    /// group in the host's own order.
    public var launchableOffers: [HarnessOffer] {
        let launchable = harnessOffers.filter(\.isLaunchable)
        return launchable.filter { $0.launchMode == .structured }
            + launchable.filter { $0.launchMode != .structured }
    }

    /// Rows the host lists but iOS cannot launch, with the reason. Rows the
    /// host has merely switched off are left out, as they always were.
    public var unavailableOffers: [(offer: HarnessOffer, reason: String)] {
        harnessOffers.compactMap { offer in
            guard !offer.isLaunchable, let reason = offer.unavailableReason else { return nil }
            return (offer, reason)
        }
    }

    /// The one line the launch sheet shows when nothing here can launch.
    public var launchUnavailableReason: String? {
        guard launchableOffers.isEmpty else { return nil }
        if !advertisesCapabilities { return HarnessCapabilityCopy.legacyHost }
        return unavailableOffers.first?.reason ?? HarnessCapabilityCopy.noLaunchableHarness
    }
}
