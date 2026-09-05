import CryptoKit
import Foundation
import Security

/// Binds protected chat-recovery records to the exact bearer token and server
/// configuration that created them. The token itself remains exclusively in
/// `TokenStore`; this item contains a digest, normalized server URL, and a
/// random namespace identifier.
public struct RecoveryBindingStore: Sendable {
    private static let account = "chat-recovery-binding-v1"
    private static let version = 1
    /// Keychain update/add is not a single API transaction. Serialize this
    /// metadata item's compare-and-replace sequence within the process so two
    /// foreground configuration attempts cannot mint competing namespaces.
    private static let operationLock = NSLock()

    private let service: String

    public init(service: String = TokenStore.defaultService) {
        self.service = service
    }

    /// Returns the existing binding only when it belongs to this exact token
    /// and server. A missing or mismatched item starts a fresh namespace;
    /// explicit configuration uses `rotate` to do the same even for unchanged
    /// credentials.
    public func binding(forToken token: String, serverURL: URL, rotate: Bool) throws -> UUID {
        try Self.operationLock.withLock {
            let fingerprint = Self.tokenFingerprint(token)
            let normalizedURL = Self.normalizedServerURL(serverURL)
            if !rotate,
               let current = try load(),
               current.version == Self.version,
               current.tokenFingerprint == fingerprint,
               current.normalizedServerURL == normalizedURL {
                return current.bindingID
            }

            let replacement = BindingMetadata(
                version: Self.version,
                bindingID: UUID(),
                tokenFingerprint: fingerprint,
                normalizedServerURL: normalizedURL
            )
            try save(replacement)
            return replacement.bindingID
        }
    }

    /// Removes only recovery metadata. It never reads, reformats, or deletes
    /// the raw UTF-8 bearer-token item.
    public func clear() throws {
        try Self.operationLock.withLock {
            let status = SecItemDelete(baseQuery() as CFDictionary)
            guard status == errSecSuccess || status == errSecItemNotFound else {
                throw KeychainError.osStatus(status)
            }
        }
    }

    static func normalizedServerURL(_ url: URL) -> String {
        guard var components = URLComponents(url: url, resolvingAgainstBaseURL: false) else {
            return url.absoluteString
        }
        components.scheme = components.scheme?.lowercased()
        components.host = components.host?.lowercased()
        components.fragment = nil
        components.query = nil
        if components.path == "/" {
            components.path = ""
        }
        return components.string ?? url.absoluteString
    }

    private func baseQuery() -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: Self.account,
        ]
    }

    private func load() throws -> BindingMetadata? {
        var query = baseQuery()
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne

        var result: AnyObject?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        if status == errSecItemNotFound {
            return nil
        }
        guard status == errSecSuccess, let data = result as? Data else {
            throw KeychainError.osStatus(status)
        }
        return try JSONDecoder().decode(BindingMetadata.self, from: data)
    }

    private func save(_ metadata: BindingMetadata) throws {
        let data = try JSONEncoder().encode(metadata)
        let update = [kSecValueData as String: data]
        let updateStatus = SecItemUpdate(baseQuery() as CFDictionary, update as CFDictionary)
        if updateStatus == errSecSuccess {
            return
        }
        guard updateStatus == errSecItemNotFound else {
            throw KeychainError.osStatus(updateStatus)
        }

        var addQuery = baseQuery()
        addQuery[kSecValueData as String] = data
        addQuery[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        let addStatus = SecItemAdd(addQuery as CFDictionary, nil)
        guard addStatus == errSecSuccess else {
            throw KeychainError.osStatus(addStatus)
        }
    }

    private static func tokenFingerprint(_ token: String) -> String {
        SHA256.hash(data: Data(token.utf8)).map { String(format: "%02x", $0) }.joined()
    }
}

private struct BindingMetadata: Codable, Sendable {
    let version: Int
    let bindingID: UUID
    let tokenFingerprint: String
    let normalizedServerURL: String
}
