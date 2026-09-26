import Photos
import CryptoKit
import SwiftUI
import OSLog

/// PhotoKit reads the Mac's System Photo Library and downloads cloud-only originals.
/// No Photos change requests or deletion APIs are used.
@MainActor final class PhotosImporting: ObservableObject {
    static let shared = PhotosImporting()
    @Published private(set) var enabled = UserDefaults.standard.bool(forKey: "photos-import-enabled")
    @Published var destination: URL = URL(fileURLWithPath: UserDefaults.standard.string(forKey: "photos-import-destination") ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Media/Originals/iCloud").path) {
        didSet { UserDefaults.standard.set(destination.path, forKey: "photos-import-destination") }
    }
    @Published var fromDate: Date = (UserDefaults.standard.object(forKey: "photos-import-from") as? Date) ?? PhotosExporter.yearRange().start {
        didSet { UserDefaults.standard.set(fromDate, forKey: "photos-import-from") }
    }
    @Published private(set) var running = false
    @Published private(set) var status = "Import originals from your synced Photos library." {
        didSet { Logger(subsystem: "local.mami.prototype", category: "PhotosImport").notice("\(self.status, privacy: .public)") }
    }
    @Published private(set) var transferred = 0
    @Published private(set) var error: String? {
        didSet { if let error { Logger(subsystem: "local.mami.prototype", category: "PhotosImport").error("\(error, privacy: .public)") } }
    }
    private var timer: Task<Void, Never>?
    private var cancellation: PhotosCancellation?

    init() {
        // Persist the initial January 1 default; it is a start date, not a rolling year filter.
        if UserDefaults.standard.object(forKey: "photos-import-from") == nil {
            UserDefaults.standard.set(fromDate, forKey: "photos-import-from")
        }
    }

    func startAutomatic() {
        guard !CommandLine.arguments.contains("--ui-test"), timer == nil else { return }
        timer = Task {
            while !Task.isCancelled {
                if enabled { await scan() }
                do { try await Task.sleep(for: .seconds(300)) } catch { return }
            }
        }
    }
    func enable() {
        UserDefaults.standard.set(fromDate, forKey: "photos-import-from")
        Task {
            let authorization = await PHPhotoLibrary.requestAuthorization(for: .readWrite)
            guard authorization == .authorized || authorization == .limited else {
                error = "Allow Mami access in System Settings → Privacy & Security → Photos."
                return
            }
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
            try Catalog.standard.registerMediaRoot(destination)
            let incoming = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Media/Incoming/.mami-photos")
            let pipeline = PhotosPipeline(cancellation: cancellation)
            let producer = Task.detached(priority: .utility) { [self] in
                do {
                    let report = try PhotosExporter.export(to: incoming, range: range, cancellation: cancellation, ready: { url, size in
                        try pipeline.enqueue(url, size: size)
                    }) { message in
                        Task { @MainActor in self.status = message }
                    }
                    pipeline.finish()
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
            status = "Photos checked · \(report.downloaded) resources fetched · \(transferred) originals saved and verified"
            if report.unsupported > 0 { status += " · \(report.unsupported) unsupported resources left in Photos" }
            if !report.failures.isEmpty { error = "\(report.failures.count) Photos resources need attention. \(report.failures[0])" }
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
        var downloaded = 0
        var unsupported = 0
        var failures: [String] = []
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
        while let data = try handle.read(upToCount: 4 * 1024 * 1024), !data.isEmpty { hash.update(data: data) }
        return hash.finalize().map { String(format: "%02x", $0) }.joined()
    }
    static func export(to root: URL, range: DateInterval, cancellation: PhotosCancellation,
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
        for url in try fm.contentsOfDirectory(at: receipts, includingPropertiesForKeys: nil) where url.pathExtension == "json" {
            let receipt = try JSONDecoder().decode(Receipt.self, from: Data(contentsOf: url))
            let key = url.deletingPathExtension().lastPathComponent
            if receipt.imported == true {
                try Catalog.standard.recordPhotosImport(resource: key, digest: receipt.digest, size: receipt.size)
                history.insert(key)
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
            report.failures.append("Photos access is unavailable for this app build. Re-enable Photos import in Settings to request access. Completed downloads can still be saved.")
            return report
        }
        let assets = PHAsset.fetchAssets(with: fetchOptions(range: range))
        for index in 0..<assets.count {
            try cancellation.check()
            let asset = assets.object(at: index)
            guard let date = asset.creationDate, date >= range.start, date < range.end else { continue }
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
                progress("Fetching Photos original \(index + 1) of \(assets.count) · \(resource.originalFilename)")
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
        return report
    }

    private static func download(_ resource: PHAssetResource, to file: URL, cancellation: PhotosCancellation) throws -> String {
        guard FileManager.default.createFile(atPath: file.path, contents: nil) else { throw AppError.message("Cannot create Photos download") }
        let handle = try FileHandle(forWritingTo: file)
        defer { try? handle.close() }
        let options = PHAssetResourceRequestOptions(); options.isNetworkAccessAllowed = true
        let finished = DispatchSemaphore(value: 0)
        let state = PhotosDownloadState(handle: handle, cancellation: cancellation)
        PHAssetResourceManager.default().requestData(for: resource, options: options, dataReceivedHandler: { state.receive($0) }, completionHandler: {
            state.complete($0); finished.signal()
        })
        finished.wait()
        return try state.result()
    }
}

private final class PhotosDownloadState: @unchecked Sendable {
    private let lock = NSLock()
    private let handle: FileHandle
    private let cancellation: PhotosCancellation
    private var hash = SHA256()
    private var error: Error?
    init(handle: FileHandle, cancellation: PhotosCancellation) { self.handle = handle; self.cancellation = cancellation }
    func receive(_ data: Data) {
        lock.lock(); defer { lock.unlock() }
        guard error == nil else { return }
        do { try cancellation.check(); try handle.write(contentsOf: data); hash.update(data: data) }
        catch { self.error = error }
    }
    func complete(_ failure: Error?) { lock.lock(); defer { lock.unlock() }; if let failure { error = failure } }
    func result() throws -> String {
        lock.lock(); defer { lock.unlock() }
        if let error { throw error }
        try cancellation.check(); try handle.synchronize()
        return hash.finalize().map { String(format: "%02x", $0) }.joined()
    }
}
