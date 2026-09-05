import Foundation
import Security

/// Stores the API bearer token in the Keychain under `kSecClassGenericPassword`,
/// account "api-token", accessible after first unlock (so background refresh can
/// read it while the device is locked-after-first-unlock).
///
/// The token is never written to `UserDefaults` or logged.
public struct TokenStore: Sendable {
    public static let defaultService = "com.arnab.drover.token"
    public let service: String
    private static let account = "api-token"
    private let updateStatusOverride: OSStatus?

    public init(service: String = Self.defaultService) {
        self.service = service
        updateStatusOverride = nil
    }

    /// Test-only fault seam. Production callers use the public initializer,
    /// which always invokes Security directly.
    init(service: String, updateStatusOverride: OSStatus?) {
        self.service = service
        self.updateStatusOverride = updateStatusOverride
    }

    private func baseQuery() -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: Self.account
        ]
    }

    /// Upserts the raw UTF-8 token. Existing credentials are updated in place
    /// so a failed replacement leaves the working item untouched.
    public func save(_ token: String) throws {
        guard let data = token.data(using: .utf8) else {
            throw KeychainError.encodingFailed
        }

        let updateAttributes: [String: Any] = [
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlock,
        ]
        let updateStatus = updateStatusOverride ?? SecItemUpdate(
            baseQuery() as CFDictionary,
            updateAttributes as CFDictionary
        )
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

    public func load() -> String? {
        var query = baseQuery()
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne

        var result: AnyObject?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        guard status == errSecSuccess,
              let data = result as? Data,
              let token = String(data: data, encoding: .utf8)
        else {
            return nil
        }
        return token
    }

    public func delete() throws {
        let status = SecItemDelete(baseQuery() as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw KeychainError.osStatus(status)
        }
    }
}

public enum KeychainError: Error, Equatable {
    case encodingFailed
    case osStatus(OSStatus)
}
