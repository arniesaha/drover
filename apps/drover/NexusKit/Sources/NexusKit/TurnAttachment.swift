import Foundation

/// One image attached to an outgoing turn, already downscaled/encoded by
/// the UI layer. `mediaType` is a MIME type the server maps to a file
/// extension (image/jpeg, image/png, image/gif, image/webp).
public struct TurnAttachment: Sendable, Equatable {
    public var mediaType: String
    public var data: Data

    public init(mediaType: String, data: Data) {
        self.mediaType = mediaType
        self.data = data
    }
}
