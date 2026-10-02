import Foundation
import Darwin
import MamiCore

/// User-requested deletion: originals go to the Trash, then the library forgets them.
/// Finder's Put Back restores an original; the next scan indexes it again. Imports
/// skip recorded deletions so a camera card cannot copy a deleted take back.
enum MediaDeletion {
    static let workerDeadline: Duration = .seconds(60)
    private static let exitGrace: Duration = .seconds(2)

    struct Outcome: Sendable {
        let assets: Set<String>
        /// Original location → Trash location.
        let trashed: [URL: URL]
    }

    static func moveToTrash(_ items: [Media], catalog: Catalog = .standard, configuration: Configuration) async throws -> Outcome {
        let assets = Set(items.map(\.assetID))
        guard !assets.isEmpty else { return Outcome(assets: [], trashed: [:]) }
        let recorded = try await Task.detached { try catalog.media(forAssetIDs: Array(assets)) }.value
        var originals: [URL] = []
        for url in (items + recorded).map(\.url) where url.isFileURL && !originals.contains(url) { originals.append(url) }
        var trashed: [URL: URL] = [:]
        do {
            for original in originals where FileManager.default.fileExists(atPath: original.path) {
                var location: NSURL?
                try FileManager.default.trashItem(at: original, resultingItemURL: &location)
                guard let location = location as URL? else { throw AppError.message("The Trash did not report where \(original.lastPathComponent) went") }
                trashed[original] = location
            }
            try await forget(assets, trashed: trashed, catalog: catalog, configuration: configuration)
        } catch {
            let unrestored = restore(trashed)
            let detail = unrestored.isEmpty ? "Nothing was deleted." : "Restore from the Trash: \(unrestored.map(\.lastPathComponent).joined(separator: ", "))."
            throw AppError.message("Could not move media to the Trash: \(error.localizedDescription) \(detail)")
        }
        return Outcome(assets: assets, trashed: trashed)
    }

    private static func forget(_ assets: Set<String>, trashed: [URL: URL], catalog: Catalog, configuration: Configuration) async throws {
        let process = Process(), output = Pipe()
        process.executableURL = configuration.python
        var arguments = ["-B", configuration.worker.deletingLastPathComponent().appendingPathComponent("media_deletion.py").path,
                         "--catalog", catalog.database.path]
        for asset in assets.sorted() { arguments += ["--asset", asset] }
        for (original, location) in trashed.sorted(by: { $0.key.path < $1.key.path }) { arguments += ["--trashed", original.path, location.path] }
        process.arguments = arguments
        process.environment = configuration.workerEnvironment
        process.standardOutput = output
        process.standardError = AppDiagnostics.workerErrorOutput
        try process.run()
        let reply = await Task.detached { () -> Data? in
            var reader = LineReader(handle: output.fileHandleForReading)
            return try? reader.readLine(timeout: MediaDeletion.workerDeadline)
        }.value
        guard await ProcessExit.reap(process, grace: exitGrace), ProcessExit.succeeded(process) == true,
              let reply, let result = try? JSONSerialization.jsonObject(with: reply) as? [String: Any] else {
            throw AppError.message("The library could not record the deletion.")
        }
        if let warnings = result["warnings"] as? [String], !warnings.isEmpty {
            fputs("Media deletion kept import staging links: \(warnings.joined(separator: "; "))\n", stderr)
        }
    }

    /// Best effort; returns originals that could not be put back.
    private static func restore(_ trashed: [URL: URL]) -> [URL] {
        trashed.compactMap { original, location in
            (try? FileManager.default.moveItem(at: location, to: original)) == nil ? original : nil
        }
    }
}
