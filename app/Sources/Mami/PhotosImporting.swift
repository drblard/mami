import Photos
import CryptoKit
import SwiftUI
import OSLog

/// PhotoKit reads the Mac's System Photo Library and downloads cloud-only originals.
/// No Photos change requests or deletion APIs are used.
@MainActor @Observable final class PhotosImporting {
    static let shared = PhotosImporting()
    private(set) var enabled = UserDefaults.standard.bool(forKey: "photos-import-enabled")
    var destination: URL = URL(fileURLWithPath: UserDefaults.standard.string(forKey: "photos-import-destination") ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Media/Originals/iCloud").path) {
        didSet { UserDefaults.standard.set(destination.path, forKey: "photos-import-destination") }
    }
    var fromDate: Date = (UserDefaults.standard.object(forKey: "photos-import-from") as? Date) ?? PhotosExporter.yearRange().start {
        didSet { UserDefaults.standard.set(fromDate, forKey: "photos-import-from") }
    }
    private(set) var running = false
    private(set) var status = "Import originals from your synced Photos library." {
        didSet { Logger(subsystem: "local.mami.prototype", category: "PhotosImport").notice("\(self.status, privacy: .public)") }
    }
    private(set) var transferred = 0
    private(set) var needsPhotosAccess = false
    private(set) var error: String? {
        didSet { if let error { Logger(subsystem: "local.mami.prototype", category: "PhotosImport").error("\(error, privacy: .public)") } }
    }
    private var timer: Task<Void, Never>?
    private var cancellation: PhotosCancellation?
    private let changes = PhotosChangeTracker()
    private var observer: PhotosLibraryObserver?
    private var changeScan: Task<Void, Never>?
    static let passInterval: Duration = .seconds(300)
    /// Collects a burst of library changes (an iCloud sync batch) into one pass.
    static let changeDelay: Duration = .seconds(10)

    init() {
        // Persist the initial January 1 default; it is a start date, not a rolling year filter.
        if UserDefaults.standard.object(forKey: "photos-import-from") == nil {
            UserDefaults.standard.set(fromDate, forKey: "photos-import-from")
        }
    }

    func startAutomatic() {
        guard !CommandLine.arguments.contains("--ui-test"), timer == nil else { return }
        changes.requireFullPass()
        let observer = PhotosLibraryObserver { Task { @MainActor in PhotosImporting.shared.libraryChanged() } }
        PHPhotoLibrary.shared().register(observer)
        self.observer = observer
        timer = Task {
            while !Task.isCancelled {
                if enabled { PhotosSync.keepRunning(); await scan() }
                do { try await Task.sleep(for: Self.passInterval) } catch { return }
            }
        }
    }
    private func libraryChanged() {
        guard enabled, changeScan == nil else { return }
        changeScan = Task {
            try? await Task.sleep(for: Self.changeDelay)
            while running || Importing.shared.running { try? await Task.sleep(for: Self.changeDelay) }
            changeScan = nil
            await scan()
        }
    }
    func enable() {
        UserDefaults.standard.set(fromDate, forKey: "photos-import-from")
        Task {
            let authorization = await PHPhotoLibrary.requestAuthorization(for: .readWrite)
            guard authorization == .authorized || authorization == .limited else {
                needsPhotosAccess = true
                status = "Photos access required — library import is incomplete"
                error = "Allow Mami access in System Settings → Privacy & Security → Photos."
                return
            }
            needsPhotosAccess = false
            enabled = true; UserDefaults.standard.set(true, forKey: "photos-import-enabled")
            await scan()
        }
    }
    func disable() {
        enabled = false; UserDefaults.standard.set(false, forKey: "photos-import-enabled")
        cancellation?.cancel()
        if Importing.shared.photosTransfer { Importing.shared.stop() }
    }
    func scan() async {
        guard enabled, !running, !Importing.shared.running else { return }
        running = true; error = nil; transferred = 0
        let cancellation = PhotosCancellation(); self.cancellation = cancellation
        defer { running = false; self.cancellation = nil }
        do {
            let destination = self.destination
            let range = DateInterval(start: Calendar.current.startOfDay(for: fromDate), end: Date.distantFuture)
            guard FileManager.default.isWritableFile(atPath: destination.path) else {
                throw AppError.message("Choose an available, writable destination in Settings. Reconnect the destination drive if it is offline.")
            }
            try await Task.detached(priority: .utility) { try Catalog.standard.registerMediaRoot(destination) }.value
            let incoming = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Media/Incoming/.mami-photos")
            let pipeline = PhotosPipeline(cancellation: cancellation)
            let changes = self.changes
            let producer = Task.detached(priority: .utility) { [self] in
                do {
                    let library = PHPhotoLibrary.shared()
                    let token = PhotosChangeTracker.currentToken(of: library)
                    let only = token == nil ? nil : changes.changedAssets(in: library, rangeStart: range.start)
                    var report = try PhotosExporter.export(to: incoming, range: range, only: only, cancellation: cancellation, ready: { url, size in
                        try pipeline.enqueue(url, size: size)
                    }) { message in
                        Task { @MainActor in self.status = message }
                    }
                    pipeline.finish()
                    report.changeToken = token
                    report.fullPass = only == nil
                    return report
                } catch {
                    pipeline.finish(error)
                    throw error
                }
            }
            let report: PhotosExporter.Report
            do {
                while let batch = try await Task.detached(priority: .utility, operation: { try pipeline.take() }).value {
                    try cancellation.check()
                    try await Importing.shared.importPhotosFolder(incoming, destination: destination, receipts: batch.map { $0.0 })
                    transferred += batch.count
                    CatalogBackups.shared.schedule()
                    pipeline.acknowledge(batch)
                }
                report = try await producer.value
            } catch {
                cancellation.cancel()
                _ = await producer.result
                throw error
            }
            CatalogBackups.shared.schedule()
            if report.failures.isEmpty, !report.needsAccess, let token = report.changeToken {
                changes.record(token: token, rangeStart: range.start, fullPass: report.fullPass)
            } else { changes.requireFullPass() }  // Retry failed resources by examining everything.
            needsPhotosAccess = report.needsAccess
            if report.needsAccess {
                status = "Photos access required — library import is incomplete · \(transferred) staged originals saved this pass"
            } else if !report.failures.isEmpty {
                status = "Photos import incomplete — \(report.failures.count) resources need attention · \(transferred) originals saved this pass"
            } else {
                status = "Photos check complete · \(report.assets) \(report.fullPass ? "matching" : "changed") library items checked · \(report.downloaded) resources fetched · \(transferred) originals saved this pass"
            }
            if report.unsupported > 0 { status += " · \(report.unsupported) unsupported resources left in Photos" }
            if report.needsAccess { error = "Allow Photos access to continue fetching the remaining library. Completed transfers are saved." }
            else if !report.failures.isEmpty { error = "\(report.failures.count) Photos resources need attention. \(report.failures[0])" }
        } catch { self.error = error.localizedDescription; status = "Photos import needs attention" }
    }
}

final class PhotosCancellation: @unchecked Sendable {
    private let lock = NSLock()
    private var stopped = false
    func cancel() { lock.lock(); stopped = true; lock.unlock() }
    func check() throws {
        lock.lock(); let value = stopped; lock.unlock()
        if value { throw AppError.message("Photos import stopped. Completed downloads are saved.") }
    }
}

enum PhotosExporter {
    static func yearRange(now: Date = Date(), calendar: Calendar = .current) -> DateInterval {
        calendar.dateInterval(of: .year, for: now)!
    }
    static func fetchOptions(range: DateInterval) -> PHFetchOptions {
        let options = PHFetchOptions()
        options.predicate = NSPredicate(format: "creationDate >= %@ AND creationDate < %@", range.start as NSDate, range.end as NSDate)
        options.sortDescriptors = [NSSortDescriptor(key: "creationDate", ascending: false)]
        return options
    }
    struct Receipt: Codable {
        let file: String
        let digest: String
        let size: Int64
        var imported: Bool?
        var captureDate: Date?
    }
    struct Report: Sendable {
        var assets = 0
        var needsAccess = false
        var downloaded = 0
        var unsupported = 0
        var failures: [String] = []
        var fullPass = true
        var changeToken: Data?
    }
    static func markImported(_ receipts: [URL], catalog: Catalog = .standard) throws {
        for url in receipts {
            var receipt = try JSONDecoder().decode(Receipt.self, from: Data(contentsOf: url))
            try catalog.recordPhotosImport(resource: url.deletingPathExtension().lastPathComponent, digest: receipt.digest, size: receipt.size)
            receipt.imported = true
            try JSONEncoder().encode(receipt).write(to: url, options: .atomic)
        }
    }
    static func hash(_ file: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: file); defer { try? handle.close() }
        var hash = SHA256()
        // FileHandle returns autoreleased NSData storage on macOS. A long-lived
        // synchronous export task otherwise retains every chunk until it returns.
        while try autoreleasepool(invoking: { () throws -> Bool in
            guard let data = try handle.read(upToCount: 4 * 1024 * 1024), !data.isEmpty else { return false }
            hash.update(data: data)
            return true
        }) {}
        return hash.finalize().map { String(format: "%02x", $0) }.joined()
    }
    /// `only`: local identifiers to examine (changed assets), or nil for every asset in range.
    static func export(to root: URL, range: DateInterval, only: Set<String>? = nil, cancellation: PhotosCancellation,
                       ready: @escaping @Sendable (URL, Int64) throws -> Void,
                       progress: @escaping @Sendable (String) -> Void) throws -> Report {
        let fm = FileManager.default
        try fm.createDirectory(at: root, withIntermediateDirectories: true)
        let receipts = root.appendingPathComponent(".receipts")
        try fm.createDirectory(at: receipts, withIntermediateDirectories: true)
        var report = Report()
        var history = try Catalog.standard.photosHistory()
        // Past completed exports are excluded from subsequent importer passes.
        // The importer still freshly verifies any pending download or retry.
        let saved = try fm.contentsOfDirectory(at: receipts, includingPropertiesForKeys: nil)
            .filter { $0.pathExtension == "json" }
            .map { url in try autoreleasepool { (url, try JSONDecoder().decode(Receipt.self, from: Data(contentsOf: url))) } }
            .sorted { ($0.1.captureDate ?? .distantPast) > ($1.1.captureDate ?? .distantPast) }
        for (url, receipt) in saved {
            let key = url.deletingPathExtension().lastPathComponent
            if receipt.imported == true {
                // Already-recorded receipts (nearly all of them) need no locked write per pass.
                if !history.contains(key) {
                    try Catalog.standard.recordPhotosImport(resource: key, digest: receipt.digest, size: receipt.size)
                    history.insert(key)
                }
                if fm.isReadableFile(atPath: root.appendingPathComponent(receipt.file).path) { try ready(url, receipt.size) }
            }
            else if history.contains(key) {
                if fm.isReadableFile(atPath: root.appendingPathComponent(receipt.file).path) { try ready(url, receipt.size) }
                continue
            }
            else if let date = receipt.captureDate, date >= range.start, date < range.end,
                    fm.isReadableFile(atPath: root.appendingPathComponent(receipt.file).path),
                    try hash(root.appendingPathComponent(receipt.file)) == receipt.digest {
                progress("Resuming completed Photos downloads · \(URL(fileURLWithPath: receipt.file).lastPathComponent)")
                try ready(url, receipt.size)
                history.insert(key) // Queued this pass; the consumer may remove staging immediately.
            }
        }
        // Receipted independent downloads can finish transferring even if macOS
        // requires Photos permission to be renewed after an app update.
        let authorization = PHPhotoLibrary.authorizationStatus(for: .readWrite)
        guard authorization == .authorized || authorization == .limited else {
            report.needsAccess = true
            progress("Photos permission status: \(authorization.rawValue) — fetching is blocked")
            report.failures.append("Photos access is unavailable for this app build. Re-enable Photos import in Settings to request access. Completed downloads can still be saved.")
            return report
        }
        func fetch() -> PHFetchResult<PHAsset> {
            autoreleasepool {
                only.map { PHAsset.fetchAssets(withLocalIdentifiers: Array($0), options: fetchOptions(range: range)) }
                    ?? PHAsset.fetchAssets(with: fetchOptions(range: range))
            }
        }
        var assets = fetch()
        report.assets = assets.count
        var index = 0
        var refreshed = Date()
        var visited = Set<String>()
        while index < assets.count {
            try cancellation.check()
            if Date().timeIntervalSince(refreshed) >= 60 {
                assets = fetch()
                report.assets = assets.count
                index = 0; refreshed = Date()
                if assets.count == 0 { break }
            }
            try autoreleasepool {
            let asset = assets.object(at: index)
            index += 1
            guard visited.insert(asset.localIdentifier).inserted else { return }
            guard let date = asset.creationDate, date >= range.start, date < range.end else { return }
            let resources = PHAssetResource.assetResources(for: asset).filter { [.photo, .video, .pairedVideo].contains($0.type) }
            for (resourceIndex, resource) in resources.enumerated() {
                try cancellation.check()
                do {
                guard ["jpg", "jpeg", "png", "heic", "mp4", "mov"].contains(URL(fileURLWithPath: resource.originalFilename).pathExtension.lowercased()) else {
                    report.unsupported += 1; continue
                }
                let key = SHA256.hash(data: Data("\(asset.localIdentifier):\(resource.type.rawValue):\(resourceIndex):\(resource.originalFilename)".utf8)).map { String(format: "%02x", $0) }.joined()
                // Import identity survives relocation, offline archives and staging cleanup.
                if history.contains(key) { continue }
                let receiptURL = receipts.appendingPathComponent(key + ".json")
                if let data = try? Data(contentsOf: receiptURL), let receipt = try? JSONDecoder().decode(Receipt.self, from: data) {
                    if receipt.imported == true { continue }
                    if fm.isReadableFile(atPath: root.appendingPathComponent(receipt.file).path),
                       try hash(root.appendingPathComponent(receipt.file)) == receipt.digest {
                        try ready(receiptURL, receipt.size); history.insert(key); continue
                    }
                    // Preserve a damaged export; only the replacement receipt is queued.
                }
                progress("Fetching Photos original · newest first · \(date.formatted(date: .abbreviated, time: .omitted)) · \(resource.originalFilename)")
                let folder = root.appendingPathComponent(key)
                try fm.createDirectory(at: folder, withIntermediateDirectories: true)
                let name = URL(fileURLWithPath: resource.originalFilename).lastPathComponent
                let target = folder.appendingPathComponent(name)
                let temporary = receipts.appendingPathComponent(UUID().uuidString + ".partial")
                defer { try? fm.removeItem(at: temporary) }
                let digest = try download(resource, to: temporary, cancellation: cancellation)
                guard try hash(temporary) == digest else { throw AppError.message("Photos download failed read-back verification: \(name)") }
                if let date = asset.creationDate { try fm.setAttributes([.modificationDate: date], ofItemAtPath: temporary.path) }
                var final = target
                if fm.fileExists(atPath: final.path) { final = folder.appendingPathComponent(UUID().uuidString + "-" + name) }
                try fm.linkItem(at: temporary, to: final)
                let directoryFD = Darwin.open(folder.path, O_RDONLY)
                guard directoryFD >= 0 else { throw AppError.message("Cannot flush Photos export directory") }
                defer { Darwin.close(directoryFD) }
                guard fsync(directoryFD) == 0 else { throw AppError.message("Cannot flush Photos export") }
                let size = (try fm.attributesOfItem(atPath: final.path)[.size] as? NSNumber)?.int64Value ?? 0
                let receipt = Receipt(file: String(final.path.dropFirst(root.path.count + 1)), digest: digest, size: size, imported: false, captureDate: asset.creationDate)
                try JSONEncoder().encode(receipt).write(to: receiptURL, options: .atomic)
                try fm.removeItem(at: temporary)
                try ready(receiptURL, size)
                history.insert(key)
                report.downloaded += 1
                } catch {
                    try cancellation.check()
                    report.failures.append("\(resource.originalFilename): \(error.localizedDescription)")
                }
            }
            }
        }
        return report
    }

    private static func download(_ resource: PHAssetResource, to file: URL, cancellation: PhotosCancellation) throws -> String {
        guard FileManager.default.createFile(atPath: file.path, contents: nil) else { throw AppError.message("Cannot create Photos download") }
        let handle = try FileHandle(forWritingTo: file)
        defer { try? handle.close() }
        let options = PHAssetResourceRequestOptions(); options.isNetworkAccessAllowed = true
        let finished = DispatchSemaphore(value: 0)
        let state = PhotosDownloadState(handle: handle, cancellation: cancellation)
        let manager = PHAssetResourceManager.default()
        let request = manager.requestData(for: resource, options: options, dataReceivedHandler: { data in autoreleasepool { state.receive(data) } }, completionHandler: {
            state.complete($0); finished.signal()
        })
        // Stop promptly when import is turned off, and give up on an iCloud fetch that stops delivering data.
        while finished.wait(timeout: .now() + downloadPollInterval) == .timedOut {
            if state.stopped || state.stalled(after: downloadStallTimeout) {
                manager.cancelDataRequest(request)
                if finished.wait(timeout: .now() + downloadCancelGrace) == .timedOut {
                    throw AppError.message("Photos did not stop the download of \(resource.originalFilename)")
                }
                guard state.stopped else { throw AppError.message("Photos stopped delivering \(resource.originalFilename); it is retried on the next pass.") }
                return try state.result()  // Throws the import-stopped error.
            }
        }
        return try state.result()
    }
    static let downloadPollInterval: DispatchTimeInterval = .seconds(1)
    static let downloadStallTimeout: TimeInterval = 300
    static let downloadCancelGrace: DispatchTimeInterval = .seconds(30)
}

private final class PhotosDownloadState: @unchecked Sendable {
    private let lock = NSLock()
    private let handle: FileHandle
    private let cancellation: PhotosCancellation
    private var hash = SHA256()
    private var error: Error?
    private var lastProgress = Date()
    init(handle: FileHandle, cancellation: PhotosCancellation) { self.handle = handle; self.cancellation = cancellation }
    func receive(_ data: Data) {
        lock.lock(); defer { lock.unlock() }
        guard error == nil else { return }
        lastProgress = Date()
        do { try cancellation.check(); try handle.write(contentsOf: data); hash.update(data: data) }
        catch { self.error = error }
    }
    var stopped: Bool { (try? cancellation.check()) == nil }
    func stalled(after seconds: TimeInterval) -> Bool {
        lock.lock(); defer { lock.unlock() }
        return Date().timeIntervalSince(lastProgress) > seconds
    }
    func complete(_ failure: Error?) { lock.lock(); defer { lock.unlock() }; if let failure { error = failure } }
    func result() throws -> String {
        lock.lock(); defer { lock.unlock() }
        if let error { throw error }
        try cancellation.check(); try handle.synchronize()
        return hash.finalize().map { String(format: "%02x", $0) }.joined()
    }
}
