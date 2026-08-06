import Foundation
import Security

/// Stores the API bearer token in the Keychain under `kSecClassGenericPassword`,
/// account "api-token", accessible after first unlock (so background refresh can
/// read it while the device is locked-after-first-unlock).
///
/// The token is never written to `UserDefaults` or logged.
public struct TokenStore: Sendable {
    private let service: String
    private static let account = "api-token"

    public init(service: String = "com.arnab.drover.token") {
        self.service = service
    }

    private func baseQuery() -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: Self.account
        ]
    }

    /// Upserts the token (delete-then-add).
    public func save(_ token: String) throws {
        guard let data = token.data(using: .utf8) else {
            throw KeychainError.encodingFailed
        }

        // Upsert: remove any existing item first, then add fresh. Ignore
        // errSecItemNotFound since that just means there was nothing to delete.
        let deleteStatus = SecItemDelete(baseQuery() as CFDictionary)
        guard deleteStatus == errSecSuccess || deleteStatus == errSecItemNotFound else {
            throw KeychainError.osStatus(deleteStatus)
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
