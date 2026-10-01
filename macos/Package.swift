// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "HorizonVoice",
    defaultLocalization: "fr",
    platforms: [.macOS(.v14)],
    products: [.executable(name: "HorizonVoice", targets: ["HorizonVoice"])],
    targets: [
        .executableTarget(name: "HorizonVoice"),
        .testTarget(name: "HorizonVoiceTests", dependencies: ["HorizonVoice"])
    ],
    swiftLanguageModes: [.v5]
)
