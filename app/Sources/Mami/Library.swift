import Foundation
import AppKit
import CryptoKit

struct Sample: Codable, Sendable {
    let path: String
    let kind: String
    let timestamp: Double?
    let frame: String
    let score: Double?
    let evidence: String?
}

struct Manifest: Decodable {
    let root: String
    let samples: [Sample]
}

struct CaptureMetadata: Codable, Sendable {
    let date: String
    let details: [String]
    let location: String?
    let duration: Double?
    let tags: [String]
    let technical: [String]
    let sortDate: String
    var subtitle: String { ([location].compactMap { $0 } + details).joined(separator: " · ") }
}

struct Media: Identifiable, Codable, Sendable {
    var id: String { path }
    let path: String
    let kind: String
    let url: URL
    let frames: [Sample]
    let match: Sample
    let metadata: CaptureMetadata?
    let assetID: String
    var title: String { url.lastPathComponent }
}

struct Configuration {
    let index: URL
    let python: URL
    let worker: URL
    let speech: String?

    static func load() throws -> Configuration {
        let env = ProcessInfo.processInfo.environment
        let home = FileManager.default.homeDirectoryForCurrentUser
        let resources = Bundle.main.resourceURL ?? URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
        let configURL = resources.appendingPathComponent("configuration.json")
        let defaults = (try? JSONDecoder().decode([String: String].self, from: Data(contentsOf: configURL))) ?? [:]
        guard let index = env["MAMI_INDEX"] ?? defaults["index"] else {
            throw AppError.message("No visual index configured. Set MAMI_INDEX or bundle configuration.json.")
        }
        return Configuration(
            index: URL(fileURLWithPath: index),
            python: URL(fileURLWithPath: env["MAMI_PYTHON"] ?? home.appendingPathComponent("mami-lab/.venv/bin/python").path),
            worker: URL(fileURLWithPath: env["MAMI_WORKER"] ?? resources.appendingPathComponent("search_worker.py").path),
            speech: env["MAMI_SPEECH"] ?? defaults["speech"])
    }
}

enum AppError: LocalizedError {
    case message(String)
    var errorDescription: String? { if case .message(let text) = self { return text }; return nil }
}

actor SearchWorker {
    private var process: Process?
    private var input: FileHandle?
    private var output: FileHandle?
    private var buffer = Data()

    func start(_ config: Configuration) throws {
        let task = Process()
        task.executableURL = config.python
        task.arguments = [config.worker.path, "--index", config.index.path]
        task.arguments! += ["--catalog", Catalog.standard.database.path]
        if let speech = config.speech { task.arguments! += ["--speech", speech] }
        var env = ProcessInfo.processInfo.environment
        env["HF_HUB_OFFLINE"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        task.environment = env
        let stdin = Pipe(), stdout = Pipe()
        task.standardInput = stdin
        task.standardOutput = stdout
        task.standardError = FileHandle.standardError
        try task.run()
        process = task
        input = stdin.fileHandleForWriting
        output = stdout.fileHandleForReading
        let ready = try JSONDecoder().decode(Ready.self, from: readLine())
        guard ready.ready else { throw AppError.message("Search model could not start.") }
    }

    private struct Ready: Decodable { let ready: Bool }
    struct Reply: Decodable, Sendable {
        let hits: [Sample]?
        let elapsed: Double?
        let error: String?
    }

    func search(_ query: String, mode: String = "visual") throws -> Reply {
        guard let input, process?.isRunning == true else { throw AppError.message("Search process is not running.") }
        var data = try JSONSerialization.data(withJSONObject: ["query": query, "mode": mode])
        data.append(10)
        try input.write(contentsOf: data)
        let reply = try JSONDecoder().decode(Reply.self, from: readLine())
        if let error = reply.error { throw AppError.message(error) }
        return reply
    }

    func stop() {
        try? input?.close()
        // EOF lets the idle worker release its model and multiprocessing handles.
        process?.waitUntilExit()
        process = nil
    }

    private func readLine() throws -> Data {
        while true {
            if let newline = buffer.firstIndex(of: 10) {
                let line = Data(buffer[..<newline])
                buffer.removeSubrange(...newline)
                return line
            }
            guard let part = output?.availableData, !part.isEmpty else {
                throw AppError.message("Search process exited. See the launch log for details.")
            }
            buffer.append(part)
        }
    }

    deinit {
        try? input?.close()
        if process?.isRunning == true { process?.terminate() }
    }
}

@MainActor final class Library: ObservableObject {
    @Published var items: [Media] = []
    @Published var query = ""
    @Published var status = "Opening library…"
    @Published var error: String?
    @Published var ready = false
    @Published var searching = false
    @Published var showingMatches = false
    @Published var mode = "visual"
    @Published var speechAvailable = false
    private var all: [Media] = []
    private var byPath: [String: Media] = [:]
    private var generation = 0
    let worker = SearchWorker()

    nonisolated static func readMedia(_ index: URL) throws -> [Media] {
        let manifest = try JSONDecoder().decode(Manifest.self, from: Data(contentsOf: index.appendingPathComponent("samples.json")))
        let groups = Dictionary(grouping: manifest.samples, by: \.path)
        let metadataURL = Bundle.main.resourceURL?.appendingPathComponent("metadata.json")
        let metadata = metadataURL.flatMap { try? Data(contentsOf: $0) }
            .flatMap { try? JSONDecoder().decode([String: CaptureMetadata].self, from: $0) } ?? [:]
        return groups.keys.sorted().map { path in
            let frames = groups[path]!.sorted { ($0.timestamp ?? 0) < ($1.timestamp ?? 0) }
            let indexedURL = URL(fileURLWithPath: manifest.root).appendingPathComponent(path)
            let url = ContentIdentity.locations[indexedURL.path].map { URL(fileURLWithPath: $0) } ?? indexedURL
            return Media(path: path, kind: frames[0].kind, url: url,
                         frames: frames, match: frames[0], metadata: metadata[path], assetID: ContentIdentity.paths[url.path] ?? url.path)
        }
    }

    func load() async {
        guard all.isEmpty else { return }
        do {
            let config = try Configuration.load()
            speechAvailable = config.speech != nil
            let media = try await Task.detached {
                let indexed: [Media]
                do {
                    indexed = try Self.readMedia(config.index)
                } catch {
                    // The persisted library can still be browsed when its index
                    // drive is unavailable. Preserve the source error if empty.
                    let saved = try Catalog.standard.media()
                    guard !saved.isEmpty else { throw error }
                    return saved
                }
                let locations = ContentIdentity.locations.sorted { $0.key < $1.key }.map { "\($0.key)\n\($0.value)" }.joined(separator: "\n")
                let seed = config.index.path + ":" + SHA256.hash(data: Data(locations.utf8)).map { String(format: "%02x", $0) }.joined()
                try Catalog.standard.synchronize(indexed, source: seed)
                return try Catalog.standard.media()
            }.value
            CatalogBackups.shared.schedule()
            all = media
            byPath = Dictionary(uniqueKeysWithValues: media.map { ($0.path, $0) })
            items = media
            status = "\(media.count) files · Loading local search model…"
            try await worker.start(config)
            ready = true
            status = "\(media.count) files · Local search ready"
            if !CommandLine.arguments.contains("--ui-test") && !CommandLine.arguments.contains("--self-test") {
                Indexing.shared.start()
            }
        } catch { self.error = error.localizedDescription; status = "Could not open library" }
    }

    func refreshCatalog() async {
        do {
            let media = try await Task.detached(priority: .utility) { try Catalog.standard.media() }.value
            all = media
            byPath = Dictionary(uniqueKeysWithValues: media.map { ($0.path, $0) })
            if !showingMatches { items = media; status = "\(media.count) files · Local search ready" }
            else {
                items = items.compactMap { matched in
                    guard let original = byPath[matched.path] else { return nil }
                    return Media(path: original.path, kind: original.kind, url: original.url, frames: original.frames,
                                 match: matched.match, metadata: original.metadata, assetID: original.assetID)
                }
            }
            CatalogBackups.shared.schedule()
        } catch { self.error = "Could not refresh indexed media: \(error.localizedDescription)" }
    }

    @discardableResult func search() -> Task<Void, Never>? {
        Indexing.shared.setSearchBusy(false)
        generation += 1
        let current = generation
        let searchMode = mode
        let text = query.trimmingCharacters(in: .whitespacesAndNewlines)
        error = nil
        if text.isEmpty { items = all; searching = false; showingMatches = false; status = "\(all.count) files"; return nil }
        guard ready else { return nil }
        searching = true
        Indexing.shared.setSearchBusy(true)
        status = "Searching locally…"
        return Task {
            defer { if generation == current { Indexing.shared.setSearchBusy(false) } }
            do {
                let reply = try await worker.search(text, mode: searchMode)
                guard generation == current else { return }
                items = (reply.hits ?? []).compactMap { sample in
                    guard let original = byPath[sample.path] else { return nil }
                    return Media(path: original.path, kind: original.kind, url: original.url, frames: original.frames, match: sample, metadata: original.metadata, assetID: original.assetID)
                }
                let label = searchMode == "speech" ? "transcript matches · Automatic Romanian transcription" : "nearest matches · Matches may be approximate"
                status = "\(items.count) \(label) · \(String(format: "%.2f", reply.elapsed ?? 0))s"
                showingMatches = true
                searching = false
            } catch {
                guard generation == current else { return }
                self.error = error.localizedDescription
                searching = false
                status = "Search failed"
            }
        }
    }
}
