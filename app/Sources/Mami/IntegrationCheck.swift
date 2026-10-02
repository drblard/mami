import AppKit
import Foundation

/// Launch flags that run one integration check in an accessory app instead of the
/// library. Each check prints its own progress; failure exits non-zero.
enum IntegrationCheck {
    struct Check: Sendable {
        let label: String
        /// Whether the flag is followed by the check's (new) output directory.
        var takesDirectory = true
        var cleanup: @MainActor @Sendable () -> Void = {}
        let run: @MainActor @Sendable (URL) async throws -> Void
    }

    private static let stopFaces: @MainActor @Sendable () -> Void = { Indexing.faces.stop() }

    static let all: [String: Check] = [
        "--catalog-test": Check(label: "CATALOG TEST") { try checkCatalog(at: $0) },
        "--worker-pipe-test": Check(label: "WORKER PIPE TEST", takesDirectory: false) { _ in
            try WorkerPipe.check()
            try SearchMaintenance.checkShutdown()
            try await SearchMaintenance.checkExitAfterOutputCloses()
        },
        "--worker-lifecycle-test": Check(label: "WORKER LIFECYCLE TEST") { try await checkWorkerLifecycle(at: $0) },
        "--media-deletion-test": Check(label: "MEDIA DELETION TEST") { try await checkMediaDeletion(at: $0) },
        "--persistent-search-test": Check(label: "PERSISTENT SEARCH TEST") { try await checkPersistentSearch(at: $0) },
        "--kind-switch-test": Check(label: "KIND SWITCH TEST") { try await checkKindSwitching(at: $0) },
        "--people-test": Check(label: "PEOPLE TEST", cleanup: stopFaces) { try await checkPeopleWorkflow(at: $0) },
        "--people-sequence-test": Check(label: "PEOPLE SEQUENCE TEST", cleanup: stopFaces) { try await checkPeopleSequence(at: $0) },
        "--face-lifecycle-test": Check(label: "FACE LIFECYCLE TEST", cleanup: stopFaces) { try await checkFaceLifecycle(at: $0) },
        "--photos-memory-test": Check(label: "PHOTOS MEMORY TEST") { try PhotosMemoryCheck.run(file: $0) },
        "--scan-ui-test": Check(label: "SCAN UI TEST", cleanup: { Indexing.shared.stop(); Indexing.previews.stop() }) { try await UIChecks.scanUITest($0) },
        "--ui-test": Check(label: "UI TEST") { try await UIChecks.uiTest($0) },
        "--self-test": Check(label: "SELF-TEST", takesDirectory: false) { _ in try await UIChecks.selfTest() },
    ]

    /// The requested check and its argument, if the launch arguments name one.
    static func requested(in arguments: [String]) -> (Check, URL)? {
        for (index, argument) in arguments.enumerated() {
            guard let check = all[argument] else { continue }
            if !check.takesDirectory { return (check, URL(fileURLWithPath: FileManager.default.currentDirectoryPath)) }
            guard arguments.indices.contains(index + 1) else { return nil }
            return (check, URL(fileURLWithPath: arguments[index + 1]))
        }
        return nil
    }

    @MainActor static func run(_ check: Check, _ argument: URL) -> Never {
        NSApplication.shared.setActivationPolicy(.accessory)
        Task {
            do { try await check.run(argument); exit(0) }
            catch { check.cleanup(); fputs("\(check.label) FAILED: \(error)\n", stderr); exit(1) }
        }
        NSApplication.shared.run()
        exit(0)
    }
}
