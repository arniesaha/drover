// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "NexusKit",
    platforms: [.iOS(.v18)],
    products: [.library(name: "NexusKit", targets: ["NexusKit"])],
    targets: [.target(name: "NexusKit")]
)
