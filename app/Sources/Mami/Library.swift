import Foundation
import Observation
import Darwin
import MamiCore
import AppKit
import CryptoKit
import ImageIO

enum MediaFormat: String, CaseIterable, Identifiable, Sendable {
    case all, vertical, horizontal, square, unknown
    var id: String { rawValue }
    var label: String {
        switch self {
        case .all: return "All shapes"
        case .vertical: return "Vertical"
        case .horizontal: return "Horizontal"
        case .square: return "Square"
        case .unknown: return "Unknown shape"
        }
    }
    var guidance: String {
        switch self {
        case .all: return "Filter by the shape of the original footage"
        case .vertical: return "Reels, TikTok & Shorts · Taller than wide"
        case .horizontal: return "YouTube & widescreen · Wider than tall"
        case .square: return "Social feeds · Equal width and height"
        case .unknown: return "No readable preview to determine the shape"
        }
    }
    static func classify(width: Double, height: Double, orientation: Int = 1) -> MediaFormat {
        guard width > 0, height > 0, width.isFinite, height.isFinite else { return .unknown }
        let ratio = (5...8).contains(orientation) ? height / width : width / height
        // Allow one-pixel rounding in downscaled previews, not near-square crops.
        if abs(width - height) <= 1 { return .square }
        return ratio < 1 ? .vertical : .horizontal
    }
    static func read(_ media: [Media]) -> [String: MediaFormat] {
        Dictionary(uniqueKeysWithValues: media.map { item in
            if let cached = item.cachedFormat, let format = MediaFormat(rawValue: cached) { return (item.path, format) }
            if let size = item.frames.first?.sourceSize, size.count == 2 {
                return (item.path, classify(width: Double(size[0]), height: Double(size[1])))
            }
            if let crop = item.frames.first?.crop, crop.count == 4 {
                return (item.path, classify(width: Double(crop[2]), height: Double(crop[3])))
            }
            guard let frame = item.frames.first,
                  let source = CGImageSourceCreateWithURL(URL(fileURLWithPath: frame.frame) as CFURL, nil),
                  let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
                  let width = properties[kCGImagePropertyPixelWidth] as? NSNumber,
                  let height = properties[kCGImagePropertyPixelHeight] as? NSNumber else { return (item.path, .unknown) }
            let orientation = (properties[kCGImagePropertyOrientation] as? NSNumber)?.intValue ?? 1
            return (item.path, classify(width: width.doubleValue, height: height.doubleValue, orientation: orientation))
        })
    }
}

struct Sample: Codable, Sendable, Equatable {
    let path: String
    let kind: String
    let timestamp: Double?
    let frame: String
    let score: Double?
    let evidence: String?
    var crop: [Int]? = nil
    var sourceSize: [Int]? = nil
    var cacheKey: String { frame + (crop.map { "#" + $0.map(String.init).joined(separator: ",") } ?? "") }
}

struct Manifest: Decodable {
    let root: String
    let samples: [Sample]
}

struct CaptureMetadata: Codable, Sendable, Equatable {
    let date: String
    let details: [String]
    let location: String?
    let duration: Double?
    let tags: [String]
    let technical: [String]
    let sortDate: String
    let camera: String?
    let source: String?
    var subtitle: String { ([location].compactMap { $0 } + details).joined(separator: " · ") }
}

struct Media: Identifiable, Codable, Sendable, Equatable {
    var id: String { path }
    let path: String
    let kind: String
    let url: URL
    let frames: [Sample]
    let match: Sample
    let metadata: CaptureMetadata?
    let assetID: String
    var previewState: String? = nil
    var frameCount: Int? = nil
    var cachedFormat: String? = nil
    var title: String { url.lastPathComponent }
    var device: String {
        let parts = url.pathComponents
        if metadata?.source != "iCloud", let index = parts.lastIndex(of: "Originals"), parts.indices.contains(index + 1), !["iCloud", "iCloud-Photos"].contains(parts[index + 1]) {
            return parts[index + 1]
        }
        if let camera = metadata?.camera, !camera.isEmpty { return camera }
        if metadata?.camera == nil, let details = metadata?.details, details.count > 1, let camera = details.last { return camera }
        if metadata?.source == "iCloud" || parts.contains("iCloud") || parts.contains("iCloud-Photos") { return "iCloud · Device unavailable" }
        return "Unknown device"
    }
}

struct Configuration: Sendable {
    let index: URL
    let python: URL
    let worker: URL
    let speech: String?
    var nativeEncoder: String? = nil
    var packedIndex: String? = nil
    var searchProjection: String? = nil
    var packPreviews = false
    var standardLayout = false
    var launchEnvironment: [String: String]? = nil
    var workerEnvironment: [String: String] {
        var result = launchEnvironment ?? ProcessInfo.processInfo.environment
        for key in ["PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"] { result.removeValue(forKey: key) }
        result["PYTHONNOUSERSITE"] = "1"
        result["PYTHONDONTWRITEBYTECODE"] = "1"
        let paths = AppPaths(environment: result)
        result["MAMI_SUPPORT_ROOT"] = paths.support.path
        if result["MAMI_CACHE_ROOT"] == nil {
            if let catalog = result["MAMI_CATALOG"], URL(fileURLWithPath: catalog).resolvingSymlinksInPath() != paths.catalog.resolvingSymlinksInPath() {
                result["MAMI_CACHE_ROOT"] = URL(fileURLWithPath: catalog).appendingPathComponent("runtime-cache").path
            } else { result["MAMI_CACHE_ROOT"] = paths.caches.path }
        }
        let helpers = worker.deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent("Helpers")
        result["PATH"] = helpers.path + ":" + python.deletingLastPathComponent().path + ":/usr/bin:/bin:/usr/sbin:/sbin"
        return result
    }

    static func load(environment env: [String: String] = ProcessInfo.processInfo.environment, resourceDirectory: URL? = nil) throws -> Configuration {
        let paths = AppPaths(environment: env)
        let resources = resourceDirectory ?? Bundle.main.resourceURL ?? URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
        let configURL = resources.appendingPathComponent("configuration.json")
        let defaults = (try? JSONDecoder().decode([String: String].self, from: Data(contentsOf: configURL))) ?? [:]
        let standardLayout = defaults["layout"] == "standard-v1"
        guard let index = env["MAMI_INDEX"] ?? (standardLayout ? paths.legacyVisual.path : defaults["index"]) else {
            throw AppError.message("No visual index configured. Set MAMI_INDEX or bundle configuration.json.")
        }
        let isolated = env["MAMI_CATALOG"].map { URL(fileURLWithPath: $0).resolvingSymlinksInPath() != paths.catalog.resolvingSymlinksInPath() } ?? false
        let packed = env["MAMI_PACKED_INDEX"] ?? (isolated ? nil : standardLayout ? paths.search.appendingPathComponent("Vectors").path : defaults["packed_index"])
        let projection = env["MAMI_SEARCH_PROJECTION"] ?? (isolated ? nil : standardLayout ? paths.search.appendingPathComponent("search.sqlite").path : defaults["search_projection"])
        return Configuration(
            index: URL(fileURLWithPath: index),
            python: URL(fileURLWithPath: env["MAMI_PYTHON"] ?? resources.deletingLastPathComponent().appendingPathComponent("Helpers/MamiPython").path),
            worker: URL(fileURLWithPath: env["MAMI_WORKER"] ?? resources.appendingPathComponent("search_worker.py").path),
            speech: env["MAMI_SPEECH"] ?? (standardLayout ? paths.legacySpeech.path : defaults["speech"]),
            nativeEncoder: env["MAMI_NATIVE_ENCODER"] ?? (standardLayout ? resources.appendingPathComponent("TextEncoder").path : defaults["native_encoder"]),
            packedIndex: packed,
            searchProjection: projection,
            packPreviews: packed != nil && projection != nil && (env["MAMI_PACK_PREVIEWS"] ?? defaults["pack_previews"]) == "1",
            standardLayout: standardLayout, launchEnvironment: env)
    }
}

enum AppError: LocalizedError {
    case message(String)
    var errorDescription: String? { if case .message(let text) = self { return text }; return nil }
}

actor SearchWorker {
    private enum Deadlines {
        static let startup: Duration = .seconds(60)
        static let query: Duration = .seconds(10)
        static let gracefulShutdown: Duration = .seconds(2)
        static let terminationGrace: Duration = .seconds(1)
    }
    private var process: Process?
    private var input: FileHandle?
    private var output: FileHandle?
    private var reader: LineReader?
    private var configuration: Configuration?
    private var idleTask: Task<Void, Never>?
    private var activityGeneration = 0
    private static let idleTimeout: Duration = .seconds(60)
    var isRunning: Bool { process?.isRunning == true }

    func start(_ config: Configuration) throws {
        abortWorker()
        configuration = config
    }

    private func launch(_ config: Configuration) throws {
        let task = Process()
        task.qualityOfService = .userInitiated
        task.executableURL = config.python
        task.arguments = [config.worker.path, "--index", config.index.path]
        task.arguments! += ["--catalog", Catalog.standard.database.path]
        if let speech = config.speech { task.arguments! += ["--speech", speech] }
        if let encoder = config.nativeEncoder { task.arguments! += ["--native-encoder", encoder] }
        if let packed = config.packedIndex { task.arguments! += ["--packed-index", packed] }
        if let projection = config.searchProjection { task.arguments! += ["--projection", projection] }
        var env = config.workerEnvironment
        env["HF_HUB_OFFLINE"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        task.environment = env
        let stdin = Pipe(), stdout = Pipe()
        task.standardInput = stdin
        task.standardOutput = stdout
        task.standardError = AppDiagnostics.workerErrorOutput
        try task.run()
        process = task
        input = stdin.fileHandleForWriting
        output = stdout.fileHandleForReading
        reader = LineReader(handle: stdout.fileHandleForReading)
        do {
            let ready = try JSONDecoder().decode(Ready.self, from: readLine(timeout: Deadlines.startup))
            guard ready.ready else { throw AppError.message("Search model could not start.") }
        } catch {
            abortWorker()
            throw error
        }
    }

    private struct Ready: Decodable { let ready: Bool }
    struct Reply: Decodable, Sendable {
        let hits: [Sample]?
        let elapsed: Double?
        let error: String?
        let visual_pending: Bool?
        let visual_error: String?
    }

    func search(_ query: String, mode: String = "both", paths: [String]? = nil, scope: ProjectionReader.Scope? = nil) throws -> Reply {
        try Task.checkCancellation()
        idleTask?.cancel(); idleTask = nil
        activityGeneration += 1
        defer { scheduleIdleExit() }
        if process?.isRunning != true, let configuration { try launch(configuration) }
        guard let input, process?.isRunning == true else { throw AppError.message("Search process is not running.") }
        var request: [String: Any] = ["query": query, "mode": mode]
        if let paths { request["paths"] = paths }
        if let scope { request["scope"] = try JSONSerialization.jsonObject(with: JSONEncoder().encode(scope)) }
        var data = try JSONSerialization.data(withJSONObject: request)
        data.append(10)
        let reply: Reply
        do {
            try WorkerPipe.write(data, to: input)
            reply = try JSONDecoder().decode(Reply.self, from: readLine(timeout: Deadlines.query))
        } catch {
            abortWorker()
            throw error
        }
        if let error = reply.error { throw AppError.message(error) }
        return reply
    }

    func stop() async {
        idleTask?.cancel(); idleTask = nil
        let ending = process
        let endingOutput = output
        try? input?.close()
        process = nil
        input = nil
        output = nil
        reader = nil
        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: Deadlines.gracefulShutdown)
        while ending?.isRunning == true && clock.now < deadline {
            do { try await clock.sleep(for: .milliseconds(25)) }
            catch { break }
        }
        if ending?.isRunning == true {
            ending?.terminate()
            let terminationDeadline = clock.now.advanced(by: Deadlines.terminationGrace)
            while ending?.isRunning == true && clock.now < terminationDeadline {
                do { try await clock.sleep(for: .milliseconds(25)) }
                catch { break }
            }
            if let ending, ending.isRunning { kill(ending.processIdentifier, SIGKILL) }
        }
        try? endingOutput?.close()
    }

    private func readLine(timeout: Duration) throws -> Data {
        guard let line = try reader?.readLine(timeout: timeout) else { throw AppError.message("Search output is unavailable.") }
        return line
    }

    private func abortWorker() {
        idleTask?.cancel(); idleTask = nil
        try? input?.close()
        try? output?.close()
        // This worker only reads generated search data. A broken protocol or
        // expired deadline must not leave a stuck inference process behind.
        if let process, process.isRunning { kill(process.processIdentifier, SIGKILL) }
        process = nil
        input = nil
        output = nil
        reader = nil
    }

    deinit {
        idleTask?.cancel()
        try? input?.close()
        if process?.isRunning == true { process?.terminate() }
    }

    private func scheduleIdleExit() {
        let generation = activityGeneration
        idleTask = Task { [weak self] in
            do { try await Task.sleep(for: Self.idleTimeout) } catch { return }
            await self?.expire(generation)
        }
    }

    private func expire(_ generation: Int) async {
        guard generation == activityGeneration else { return }
        idleTask = nil
        await stop()
    }
}

@MainActor @Observable final class Library {
    var items: [Media] = []
    var query = "" {
        didSet {
            // Invalidate in-flight results immediately, before the typing debounce.
            pendingSearch?.cancel()
            generation += 1
        }
    }
    var status = "Opening library…"
    var error: String?
    var ready = false
    var searching = false
    var showingMatches = false
    var mode = "both"
    var format = MediaFormat.all
    var deviceFilter = "All devices"
    var dateEnabled = false
    var dateFrom = Calendar.current.date(from: DateComponents(year: Calendar.current.component(.year, from: Date()), month: 1, day: 1))!
    var dateThrough = Date()
    private(set) var queryDates: CaptureRange?
    var gridLocked = false {
        didSet {
            if projection != nil {
                lockedSequence = gridLocked ? latestSequence : nil
                projectedArrivals = 0
                search()
                return
            }
            if gridLocked { frozenCatalog = all }
            else { frozenCatalog = nil; search() }
        }
    }
    private var frozenCatalog: [Media]?
    private var browsingCatalog: [Media] { frozenCatalog ?? all }
    var pendingMediaCount: Int {
        if projection != nil { return projectedArrivals }
        guard let frozenCatalog else { return 0 }
        let ids = Set(frozenCatalog.map(\.assetID))
        return Set(all.filter { !ids.contains($0.assetID) }.map(\.assetID)).count
    }
    func refreshGrid() {
        if projection != nil, gridLocked { lockedSequence = latestSequence; projectedArrivals = 0 }
        else if gridLocked { frozenCatalog = all }
        search()
    }
    var manualDates: CaptureRange? { dateEnabled ? CaptureRange(from: CaptureRange.day(dateFrom), through: CaptureRange.day(dateThrough)) : nil }
    func matchesDate(_ media: Media) -> Bool {
        (manualDates?.contains(media.metadata?.sortDate) ?? true) && (queryDates?.contains(media.metadata?.sortDate) ?? true)
    }
    var devices: [String] { projection != nil ? projectedCameras : Set(all.map(\.device)).sorted() }
    func matchesDevice(_ media: Media) -> Bool { deviceFilter == "All devices" || media.device == deviceFilter }
    private(set) var formats: [String: MediaFormat] = [:]
    func matchesFormat(_ media: Media) -> Bool { format == .all || formats[media.path, default: .unknown] == format }
    var speechAvailable = false
    private var all: [Media] = []
    private var projection: ProjectionReader?
    private var pageCursor: ProjectionReader.Cursor?
    private var pageScope = ProjectionReader.Scope()
    private var pageGeneration = 0
    private var latestSequence: Int64 = 0
    private var lockedSequence: Int64?
    private var projectedCameras: [String] = []
    private var projectedEarliest: String?
    private(set) var projectedArrivals = 0
    private(set) var totalMediaCount = 0
    private(set) var loadingPage = false
    var browseKind: String? = nil
    var browseAssets: Set<String>? = nil
    var oldestFirst = false
    var usesProjection: Bool { projection != nil }
    var projectionReader: ProjectionReader? { projection }
    var canLoadMore: Bool { usesProjection && !searching && !showingMatches && all.count < totalMediaCount && pageCursor != nil }
    var catalogMedia: [Media] { all }
    var earliestCaptureDate: Date? {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyyMMdd"
        formatter.isLenient = false
        if projection != nil { return projectedEarliest.flatMap { formatter.date(from: String($0.prefix(8))) } }
        return all.compactMap { media in
            media.metadata.flatMap { formatter.date(from: String($0.sortDate.prefix(8))) }
        }.min()
    }
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
            if config.standardLayout {
                try await Task.detached { try AppPaths().prepareGeneratedDirectories() }.value
            }
            try await Task.detached { try Catalog.standard.prepareUserStore() }.value
            speechAvailable = config.speech != nil
            if let path = config.searchProjection {
                let prepared = (try? SQLDatabase(URL(fileURLWithPath: path), readOnly: true).scalar("SELECT ready FROM checkpoint WHERE id=1")) == "1"
                if !prepared {
                    status = "Preparing the search catalog…"
                    try await Task.detached(priority: .userInitiated) {
                        let process = Process()
                        process.executableURL = config.python
                        process.environment = config.workerEnvironment
                        process.arguments = ["-B", config.worker.deletingLastPathComponent().appendingPathComponent("prepare_storage.py").path,
                                             "--catalog", Catalog.standard.database.path, "--projection", path]
                        if FileManager.default.fileExists(atPath: config.index.path) { process.arguments! += ["--index", config.index.path] }
                        if let speech = config.speech, FileManager.default.fileExists(atPath: speech) { process.arguments! += ["--speech", speech] }
                        process.standardError = FileHandle.standardError
                        try process.run()
                        defer { if process.isRunning { kill(process.processIdentifier, SIGKILL) } }
                        let deadline = ContinuousClock.now.advanced(by: .seconds(60))
                        while process.isRunning && ContinuousClock.now < deadline { try await Task.sleep(for: .milliseconds(25)) }
                        if process.isRunning {
                            kill(process.processIdentifier, SIGKILL)
                            throw AppError.message("Preparing generated search data exceeded its deadline; retry to resume")
                        }
                        guard process.terminationStatus == 0 else { throw AppError.message("Could not prepare generated search data") }
                    }.value
                }
                let identity = try await Task.detached { try Catalog.standard.identity() }.value
                let reader = ProjectionReader(database: URL(fileURLWithPath: path), sourceIdentity: identity)
                let initial = try await Task.detached { try (reader.facets(), reader.page(scope: .init())) }.value
                projection = reader
                if !LaunchMode.isIntegrationCheck {
                    try SearchMaintenance.shared.start(config)
                }
                projectedCameras = initial.0.cameras
                projectedEarliest = initial.0.earliest
                latestSequence = initial.0.sequence
                totalMediaCount = initial.1.total
                pageCursor = initial.1.cursor
                all = initial.1.items
                byPath = Dictionary(uniqueKeysWithValues: all.map { ($0.path, $0) })
                formats = MediaFormat.read(all)
                items = all
                status = "\(totalMediaCount) files · Loading local search…"
                try await worker.start(config)
                ready = true
                status = "\(totalMediaCount) files · Local search ready"
                if !query.isEmpty { search() }
                if !LaunchMode.isIntegrationCheck {
                    Indexing.startAll()
                }
                return
            }
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
            formats = await Task.detached(priority: .utility) { MediaFormat.read(media) }.value
            all = media
            byPath = Dictionary(uniqueKeysWithValues: media.map { ($0.path, $0) })
            items = media
            status = "\(media.count) files · Loading local search model…"
            if !LaunchMode.isIntegrationCheck {
                Indexing.startAll()
            }
            try await worker.start(config)
            ready = true
            status = "\(media.count) files · Local search ready"
            if !query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { search() }
        } catch { self.error = error.localizedDescription; status = "Could not open library" }
    }

    /// Media moved to the Trash this session. They disappear at once; the search
    /// projection drops them when its sync catches up.
    private(set) var deletedAssets = Set<String>()
    func forget(_ assets: Set<String>) {
        deletedAssets.formUnion(assets)
        all.removeAll { assets.contains($0.assetID) }
        items.removeAll { assets.contains($0.assetID) }
        byPath = byPath.filter { !assets.contains($0.value.assetID) }
    }

    func refreshCatalog() async {
        do {
            if let projection {
                let sequence = lockedSequence
                let refreshed = try await Task.detached { try (projection.facets(), sequence.map { try projection.arrivals(after: $0) } ?? 0) }.value
                guard !Task.isCancelled else { return }
                projectedCameras = refreshed.0.cameras
                projectedEarliest = refreshed.0.earliest
                latestSequence = refreshed.0.sequence
                if projectedArrivals != refreshed.1 { projectedArrivals = refreshed.1 }
                if !showingMatches && !gridLocked {
                    let scope = projectionScope()
                    let count = max(ProjectionReader.pageSize, all.count)
                    let version = generation
                    let page = try await Task.detached { try projection.page(scope: scope, limit: count) }.value
                    guard !Task.isCancelled, generation == version, !showingMatches else { return }
                    pageGeneration += 1
                    if loadingPage { loadingPage = false }
                    all = page.items
                    byPath = Dictionary(uniqueKeysWithValues: all.map { ($0.path, $0) })
                    let readFormats = MediaFormat.read(all)
                    if formats != readFormats { formats = readFormats }
                    pageScope = scope; pageCursor = page.cursor
                    if totalMediaCount != page.total { totalMediaCount = page.total }
                    // Most refreshes during indexing change nothing visible; skip the grid update.
                    if items != all { items = all }
                } else if !showingMatches {
                    let paths = all.map(\.path)
                    let refreshed = try await Task.detached { try projection.media(paths: paths) }.value
                    guard !Task.isCancelled else { return }
                    let updated = Dictionary(uniqueKeysWithValues: refreshed.map { ($0.path,$0) })
                    all = all.map { updated[$0.path] ?? $0 }
                    byPath.merge(updated) { _, new in new }
                    let merged = formats.merging(MediaFormat.read(refreshed)) { _, new in new }
                    if formats != merged { formats = merged }
                    if items != all { items = all }
                }
                return
            }
            let media = try await Task.detached(priority: .utility) { try Catalog.standard.media() }.value
            guard !Task.isCancelled else { return }
            formats = await Task.detached(priority: .utility) { MediaFormat.read(media) }.value
            guard !Task.isCancelled else { return }
            all = media
            byPath = Dictionary(uniqueKeysWithValues: media.map { ($0.path, $0) })
            if gridLocked { /* Keep the displayed snapshot until explicit refresh. */ }
            else if !showingMatches { items = media.filter { matchesDate($0) }; status = "\(media.count) files · Local search ready" }
            else {
                items = items.compactMap { matched in
                    guard let original = byPath[matched.path] else { return nil }
                    return Media(path: original.path, kind: original.kind, url: original.url, frames: original.frames,
                                 match: matched.match, metadata: original.metadata, assetID: original.assetID)
                }
            }
        } catch { self.error = "Could not refresh indexed media: \(error.localizedDescription)" }
    }

    private var pendingSearch: Task<Void, Never>?
    private func projectionScope() -> ProjectionReader.Scope {
        let ranges = [manualDates, queryDates].compactMap { $0 }
        return ProjectionReader.Scope(camera: deviceFilter == "All devices" ? nil : deviceFilter,
                                      shape: format == .all ? nil : format.rawValue, kind: browseKind,
                                      from: ranges.map { $0.from.replacingOccurrences(of: "-", with: "") }.max(),
                                      through: ranges.map { $0.through.replacingOccurrences(of: "-", with: "") + "235959" }.min(),
                                      assets: browseAssets, arrivalThrough: lockedSequence, oldestFirst: oldestFirst)
    }

    func loadMore() async {
        guard let projection, canLoadMore, !loadingPage else { return }
        let cursor = pageCursor, scope = pageScope, current = pageGeneration
        loadingPage = true
        defer { if pageGeneration == current { loadingPage = false } }
        do {
            let page = try await Task.detached(priority: .utility) { try projection.page(scope: scope, after: cursor) }.value
            guard current == pageGeneration, !Task.isCancelled else { return }
            let known = Set(all.map(\.assetID))
            let additions = page.items.filter { !known.contains($0.assetID) }
            all += additions
            for media in additions { byPath[media.path] = media }
            formats.merge(MediaFormat.read(additions)) { _, new in new }
            pageCursor = page.cursor
            totalMediaCount = page.total
            items = all
        } catch { self.error = "Could not load more media: \(error.localizedDescription)" }
    }
    func searchAndWait() async {
        search()
        while let task = pendingSearch {
            let current = generation
            await task.value
            if current == generation { return }
        }
    }

    @discardableResult func search() -> Task<Void, Never>? {
        pendingSearch?.cancel()
        generation += 1
        let current = generation
        let searchMode = mode
        error = nil
        let parsed: DateSearch.Parsed
        do {
            parsed = try DateSearch.parse(query)
            if let dates = manualDates, dates.from > dates.through { throw AppError.message("Capture date range: start must be before end") }
        } catch { self.error = error.localizedDescription; searching = false; return nil }
        queryDates = parsed.range
        let eligible = browsingCatalog.filter { matchesFormat($0) && matchesDevice($0) && matchesDate($0) }
        let paths = projection != nil || (format == .all && deviceFilter == "All devices" && !dateEnabled && queryDates == nil && !gridLocked) ? nil : eligible.map(\.path)
        let scope = projection == nil ? nil : projectionScope()
        let text = parsed.text
        let submittedQuery = query
        if let projection, text.isEmpty {
            let scope = projectionScope()
            pageGeneration += 1
            let pageRequest = pageGeneration
            loadingPage = true
            searching = false
            showingMatches = false
            let task = Task {
                defer { if pageGeneration == pageRequest { loadingPage = false } }
                do {
                    let page = try await Task.detached(priority: .userInitiated) { try projection.page(scope: scope) }.value
                    guard generation == current, !Task.isCancelled else { return }
                    all = page.items
                    byPath = Dictionary(uniqueKeysWithValues: all.map { ($0.path, $0) })
                    formats = MediaFormat.read(all)
                    pageScope = scope
                    pageCursor = page.cursor
                    totalMediaCount = page.total
                    items = all
                    status = "\(page.total) files"
                } catch { if generation == current { self.error = error.localizedDescription } }
            }
            pendingSearch = task
            return task
        }
        if text.isEmpty { items = eligible; searching = false; showingMatches = false; status = "\(eligible.count) files"; return nil }
        if projection != nil { pageGeneration += 1; loadingPage = false }
        guard ready else { return nil }
        searching = true
        status = "Searching locally…"
        let task = Task {
            do {
                let visualDeadline = ContinuousClock.now.advanced(by: SearchTiming.visualWarmupTimeout)
                while true {
                    let reply = try await worker.search(text, mode: searchMode, paths: paths, scope: scope)
                    guard generation == current, query == submittedQuery, !Task.isCancelled else { return }
                    if let projection {
                        let requested = (reply.hits ?? []).map(\.path)
                        let media = try await Task.detached { try projection.media(paths: requested) }.value
                        guard generation == current, !Task.isCancelled else { return }
                        // Retain the loaded browse window and this reply, not
                        // every off-page match from all previous queries.
                        byPath = Dictionary(uniqueKeysWithValues: all.map { ($0.path, $0) })
                        for item in media { byPath[item.path] = item }
                        formats = MediaFormat.read(Array(byPath.values))
                    }
                    items = (reply.hits ?? []).compactMap { sample in
                        guard let original = byPath[sample.path] else { return nil }
                        return Media(path: original.path, kind: original.kind, url: original.url, frames: original.frames,
                                     match: sample, metadata: original.metadata, assetID: original.assetID,
                                     previewState: original.previewState, frameCount: original.frameCount, cachedFormat: original.cachedFormat)
                    }
                    showingMatches = true
                    if reply.visual_pending == true {
                        status = "\(items.count) transcript matches · Warming visual search…"
                        guard ContinuousClock.now < visualDeadline else {
                            throw AppError.message("Visual search did not become ready within thirty seconds. Speech search remains available.")
                        }
                        try await Task.sleep(for: SearchTiming.visualWarmupPoll)
                        try Task.checkCancellation()
                        continue
                    }
                    let label = reply.visual_error != nil ? "transcript matches · Visual search unavailable"
                        : searchMode == "speech" ? "transcript matches · Automatic Romanian transcription"
                        : searchMode == "both" ? "combined matches · Visual similarity + Romanian speech"
                        : "nearest matches · Matches may be approximate"
                    status = "\(items.count) \(label) · \(String(format: "%.2f", reply.elapsed ?? 0))s"
                    if let visualError = reply.visual_error { self.error = visualError }
                    searching = false
                    break
                }
            } catch {
                guard generation == current, query == submittedQuery, !Task.isCancelled else { return }
                self.error = error.localizedDescription
                searching = false
                status = "Search failed"
            }
        }
        pendingSearch = task
        return task
    }
}
