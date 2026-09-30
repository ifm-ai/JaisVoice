// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "HorizonVoice",
    defaultLocalization: "fr",
    platforms: [.macOS(.v14)],
    products: [.executable(name: "HorizonVoice", targets: ["HorizonVoice"])],
    dependencies: [.package(url: "https://github.com/livekit/client-sdk-swift-xcframework.git", exact: "2.16.0")],
    targets: [
        .executableTarget(name: "HorizonVoice", dependencies: [.product(name: "LiveKit", package: "client-sdk-swift-xcframework")]),
        .testTarget(name: "HorizonVoiceTests", dependencies: ["HorizonVoice"])
    ],
    swiftLanguageModes: [.v5]
)
