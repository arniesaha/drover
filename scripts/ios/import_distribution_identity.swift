#!/usr/bin/env swift
import Foundation
import Security

enum ImportError: Error {
    case usage
    case missingEnvironment
    case security(OSStatus)
}

func requiredEnvironment(_ name: String) throws -> String {
    guard let value = ProcessInfo.processInfo.environment[name], !value.isEmpty else {
        throw ImportError.missingEnvironment
    }
    return value
}

func requireSuccess(_ status: OSStatus) throws {
    guard status == errSecSuccess else {
        throw ImportError.security(status)
    }
}

func main() throws {
    let arguments = Array(CommandLine.arguments.dropFirst())
    guard arguments.count == 4,
          arguments[0] == "--p12-path",
          arguments[2] == "--keychain-path"
    else {
        throw ImportError.usage
    }

    let p12Path = arguments[1]
    let keychainPath = arguments[3]
    let keychainPassword = try requiredEnvironment("DROVER_SIGNING_KEYCHAIN_PASSWORD")
    let p12Password = try requiredEnvironment("DROVER_SIGNING_P12_PASSWORD")
    let p12Data = try Data(contentsOf: URL(fileURLWithPath: p12Path))
    let keychainPasswordBytes = Array(keychainPassword.utf8)

    var keychain: SecKeychain?
    try keychainPath.withCString { path in
        try keychainPasswordBytes.withUnsafeBytes { password in
            try requireSuccess(
                SecKeychainCreate(
                    path,
                    UInt32(password.count),
                    password.baseAddress,
                    false,
                    nil,
                    &keychain
                )
            )
        }
    }
    guard let keychain else {
        throw ImportError.security(errSecInternalComponent)
    }
    try keychainPasswordBytes.withUnsafeBytes { password in
        try requireSuccess(
            SecKeychainUnlock(
                keychain,
                UInt32(password.count),
                password.baseAddress,
                true
            )
        )
    }

    var trustedApplication: SecTrustedApplication?
    try "/usr/bin/codesign".withCString { path in
        try requireSuccess(SecTrustedApplicationCreateFromPath(path, &trustedApplication))
    }
    guard let trustedApplication else {
        throw ImportError.security(errSecInternalComponent)
    }
    var access: SecAccess?
    try requireSuccess(
        SecAccessCreate(
            "Drover distribution signing" as CFString,
            [trustedApplication] as CFArray,
            &access
        )
    )
    guard let access else {
        throw ImportError.security(errSecInternalComponent)
    }

    let importOptions: [CFString: Any] = [
        kSecImportExportPassphrase: p12Password,
        kSecImportExportKeychain: keychain,
        kSecImportExportAccess: access,
    ]
    var importedItems: CFArray?
    try requireSuccess(
        SecPKCS12Import(p12Data as CFData, importOptions as CFDictionary, &importedItems)
    )
    guard let importedItems, CFArrayGetCount(importedItems) > 0 else {
        throw ImportError.security(errSecInternalComponent)
    }
}

do {
    try main()
    print("distribution identity imported")
} catch ImportError.usage {
    FileHandle.standardError.write(
        Data("usage: import_distribution_identity.swift --p12-path PATH --keychain-path PATH\n".utf8)
    )
    exit(2)
} catch {
    FileHandle.standardError.write(Data("distribution identity import failed\n".utf8))
    exit(1)
}
