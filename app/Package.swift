// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "Mami",
    platforms: [.macOS(.v14)],
    dependencies: [
        // 6.4 requires Xcode's private _TestingInterop library, absent from CLT.
        .package(url: "https://github.com/swiftlang/swift-testing.git", revision: "swift-6.2-RELEASE")
    ],
    targets: [
        .target(name: "MamiCore"),
        .executableTarget(name: "Mami", dependencies: ["MamiCore"]),
        .testTarget(name: "MamiCoreTests", dependencies: ["MamiCore", .product(name: "Testing", package: "swift-testing")])
    ]
)
