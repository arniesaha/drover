// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "DroverKit",
    platforms: [.iOS(.v18), .macOS(.v14)],
    products: [.library(name: "DroverKit", targets: ["DroverKit"])],
    targets: [
        .target(name: "DroverKit"),
        .testTarget(
            name: "DroverKitTests",
            dependencies: ["DroverKit"],
            resources: [.copy("Fixtures")]
        ),
    ]
)
