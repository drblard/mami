import Foundation

/// User-domain storage, kept separate from installation/development directories.
struct AppPaths: Sendable {
    let support: URL
    let caches: URL
    var personal: URL { support.appendingPathComponent("Personal") }
    var catalog: URL { support.appendingPathComponent("Derived/Catalog") }
    var artifacts: URL { support.appendingPathComponent("Derived/Artifacts") }
    var search: URL { support.appendingPathComponent("Derived/Search") }
    var models: URL { support.appendingPathComponent("Models") }
    var legacyVisual: URL { support.appendingPathComponent("Derived/Legacy/Visual") }
    var legacySpeech: URL { support.appendingPathComponent("Derived/Legacy/Speech") }

    init(environment: [String: String] = ProcessInfo.processInfo.environment,
         home: URL = FileManager.default.homeDirectoryForCurrentUser) {
        let manager = FileManager.default
        let standardHome = home == manager.homeDirectoryForCurrentUser
        let applicationSupport = standardHome
            ? manager.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            : home.appendingPathComponent("Library/Application Support")
        let cacheRoot = standardHome
            ? manager.urls(for: .cachesDirectory, in: .userDomainMask)[0]
            : home.appendingPathComponent("Library/Caches")
        support = environment["MAMI_SUPPORT_ROOT"].map { URL(fileURLWithPath: $0) }
            ?? applicationSupport.appendingPathComponent("Mami")
        caches = environment["MAMI_CACHE_ROOT"].map { URL(fileURLWithPath: $0) }
            ?? cacheRoot.appendingPathComponent("Mami")
    }
}
