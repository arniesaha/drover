import Foundation
import Security

/// Stores the API bearer token in the Keychain under `kSecClassGenericPassword`,
/// account "api-token", accessible after first unlock (so background refresh can
/// read it while the device is locked-after-first-unlock).
///
/// The token is never written to `UserDefaults` or logged.
public struct TokenStore: Sendable {
    public static let defaultService = "com.arnab.drover.token"
    public static let sharedAccessGroup = "group.com.arnab.drover"
    public let service: String
    public let accessGroup: String?
    private static let account = "api-token"
    private let updateStatusOverride: OSStatus?

    public init(service: String = Self.defaultService, accessGroup: String? = nil) {
        self.service = service
        self.accessGroup = accessGroup
        updateStatusOverride = nil
    }

    /// Test-only fault seam. Production callers use the public initializer,
    /// which always invokes Security directly.
    init(service: String, accessGroup: String? = nil, updateStatusOverride: OSStatus?) {
        self.service = service
        self.accessGroup = accessGroup
        self.updateStatusOverride = updateStatusOverride
    }

    private func baseQuery(includeAccessGroup: Bool = true) -> [String: Any] {
        var query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: Self.account
        ]
        if includeAccessGroup, let accessGroup {
            query[kSecAttrAccessGroup as String] = accessGroup
        }
        return query
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
        var updateStatus = updateStatusOverride ?? SecItemUpdate(
            baseQuery() as CFDictionary,
            updateAttributes as CFDictionary
        )
        if updateStatus == errSecMissingEntitlement, accessGroup != nil {
            updateStatus = SecItemUpdate(
                baseQuery(includeAccessGroup: false) as CFDictionary,
                updateAttributes as CFDictionary
            )
        }
        if updateStatus == errSecSuccess {
            return
        }
        guard updateStatus == errSecItemNotFound else {
            throw KeychainError.osStatus(updateStatus)
        }

        var addQuery = baseQuery()
        addQuery[kSecValueData as String] = data
        addQuery[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock

        var addStatus = SecItemAdd(addQuery as CFDictionary, nil)
        if addStatus == errSecMissingEntitlement, accessGroup != nil {
            var fallbackQuery = baseQuery(includeAccessGroup: false)
            fallbackQuery[kSecValueData as String] = data
            fallbackQuery[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
            addStatus = SecItemAdd(fallbackQuery as CFDictionary, nil)
        }
        guard addStatus == errSecSuccess else {
            throw KeychainError.osStatus(addStatus)
        }
    }

    public func load() -> String? {
        var query = baseQuery()
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne

        var result: AnyObject?
        var status = SecItemCopyMatching(query as CFDictionary, &result)
        if (status == errSecItemNotFound || status == errSecMissingEntitlement), accessGroup != nil {
            var fallbackQuery = baseQuery(includeAccessGroup: false)
            fallbackQuery[kSecReturnData as String] = true
            fallbackQuery[kSecMatchLimit as String] = kSecMatchLimitOne
            status = SecItemCopyMatching(fallbackQuery as CFDictionary, &result)
        }
        guard status == errSecSuccess,
              let data = result as? Data,
              let token = String(data: data, encoding: .utf8)
        else {
            return nil
        }
        return token
    }

    public func delete() throws {
        var status = SecItemDelete(baseQuery() as CFDictionary)
        if (status == errSecMissingEntitlement), accessGroup != nil {
            status = SecItemDelete(baseQuery(includeAccessGroup: false) as CFDictionary)
        }
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw KeychainError.osStatus(status)
        }
    }
}

public enum KeychainError: Error, Equatable {
    case encodingFailed
    case osStatus(OSStatus)
}
