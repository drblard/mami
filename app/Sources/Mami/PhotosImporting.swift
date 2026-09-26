import Photos
import CryptoKit
import SwiftUI

/// PhotoKit reads the Mac's System Photo Library and downloads cloud-only originals.
/// No Photos change requests or deletion APIs are used.
@MainActor final class PhotosImporting: ObservableObject {
    static let shared = PhotosImporting()
    @Published private(set) var enabled = UserDefaults.standard.bool(forKey: "photos-import-enabled")
    @Published private(set) var running = false
    @Published private(set) var status = "Connect your synced Photos library for cable-free imports."
    @Published private(set) var error: String?
    private var timer: Task<Void, Never>?
    private var cancellation: PhotosCancellation?

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
    }
    func scan() async {
        guard enabled, !running, !Importing.shared.running else { return }
        let authorization = PHPhotoLibrary.authorizationStatus(for: .readWrite)
        guard authorization == .authorized || authorization == .limited else {
            error = "Photos access is unavailable. Enable access in System Settings."
            return
        }
        running = true; error = nil
        let cancellation = PhotosCancellation(); self.cancellation = cancellation
        defer { running = false; self.cancellation = nil }
        do {
            let incoming = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Media/Incoming/.mami-photos")
            let report = try await Task.detached(priority: .utility) { [self] in
                try PhotosExporter.export(to: incoming, cancellation: cancellation) { message in
                    Task { @MainActor in self.status = message }
                }
            }.value
            guard enabled else { status = "Automatic Photos import is off"; return }
            // Feed independent exports through the same SHA-256 verified importer.
            // This source can never request removal, irrespective of camera settings.
            if !report.pending.isEmpty {
                try await Importing.shared.importPhotosFolder(incoming, policies: report.policies)
                try await Task.detached(priority: .utility) { try PhotosExporter.markImported(report.pending) }.value
            }
            status = "Photos checked · \(report.downloaded) original resources downloaded · \(report.pending.count) independent copies verified"
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
    struct Receipt: Codable {
        let file: String
        let digest: String
        let size: Int64
        var imported: Bool?
    }
    struct Report: Sendable {
        var downloaded = 0
        var unsupported = 0
        var pending: [URL] = []
        var policies: [String: String] = [:]
        var failures: [String] = []
    }
    static func markImported(_ receipts: [URL]) throws {
        for url in receipts {
            var receipt = try JSONDecoder().decode(Receipt.self, from: Data(contentsOf: url))
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
    static func export(to root: URL, cancellation: PhotosCancellation, progress: @escaping @Sendable (String) -> Void) throws -> Report {
        let fm = FileManager.default
        try fm.createDirectory(at: root, withIntermediateDirectories: true)
        let receipts = root.appendingPathComponent(".receipts")
        try fm.createDirectory(at: receipts, withIntermediateDirectories: true)
        let assets = PHAsset.fetchAssets(with: nil)
        var report = Report()
        // Past completed exports are excluded from subsequent importer passes.
        // The importer still freshly verifies any pending download or retry.
        for url in try fm.contentsOfDirectory(at: receipts, includingPropertiesForKeys: nil) where url.pathExtension == "json" {
            let receipt = try JSONDecoder().decode(Receipt.self, from: Data(contentsOf: url))
            if receipt.imported == true { report.policies[receipt.file] = "skip" }
            else { report.pending.append(url) }
        }
        for index in 0..<assets.count {
            try cancellation.check()
            let asset = assets.object(at: index)
            let resources = PHAssetResource.assetResources(for: asset).filter { [.photo, .video, .pairedVideo].contains($0.type) }
            for (resourceIndex, resource) in resources.enumerated() {
                try cancellation.check()
                do {
                guard ["jpg", "jpeg", "png", "heic", "mp4", "mov"].contains(URL(fileURLWithPath: resource.originalFilename).pathExtension.lowercased()) else {
                    report.unsupported += 1; continue
                }
                let key = SHA256.hash(data: Data("\(asset.localIdentifier):\(resource.type.rawValue):\(resourceIndex):\(resource.originalFilename)".utf8)).map { String(format: "%02x", $0) }.joined()
                let receiptURL = receipts.appendingPathComponent(key + ".json")
                if let data = try? Data(contentsOf: receiptURL), let receipt = try? JSONDecoder().decode(Receipt.self, from: data) {
                    if receipt.imported == true { continue }
                    if fm.isReadableFile(atPath: root.appendingPathComponent(receipt.file).path),
                       try hash(root.appendingPathComponent(receipt.file)) == receipt.digest { continue }
                    // Preserve a damaged export, but do not send it to the importer.
                    report.policies[receipt.file] = "skip"
                }
                progress("Downloading Photos original \(index + 1) of \(assets.count) · \(resource.originalFilename)")
                let folder = root.appendingPathComponent(key)
                try fm.createDirectory(at: folder, withIntermediateDirectories: true)
                let name = URL(fileURLWithPath: resource.originalFilename).lastPathComponent
                let target = folder.appendingPathComponent(name)
                let temporary = receipts.appendingPathComponent(UUID().uuidString + ".partial")
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
                let receipt = Receipt(file: String(final.path.dropFirst(root.path.count + 1)), digest: digest, size: size, imported: false)
                try JSONEncoder().encode(receipt).write(to: receiptURL, options: .atomic)
                if !report.pending.contains(receiptURL) { report.pending.append(receiptURL) }
                report.downloaded += 1
                } catch {
                    try cancellation.check()
                    report.failures.append("\(resource.originalFilename): \(error.localizedDescription)")
                }
            }
        }
        // Explicitly exclude retained orphan/failed exports, including a crash
        // after publication but before its receipt. Only receipted bytes enter
        // the verified importer; files removed from Photos can still be imported.
        report.policies = [:]
        if let files = fm.enumerator(at: root, includingPropertiesForKeys: [.isRegularFileKey], options: [.skipsHiddenFiles]) {
            for case let file as URL in files where (try file.resourceValues(forKeys: [.isRegularFileKey])).isRegularFile == true {
                report.policies[String(file.path.dropFirst(root.path.count + 1))] = "skip"
            }
        }
        var verified: [URL] = []
        for url in report.pending {
            try cancellation.check()
            do {
            let receipt = try JSONDecoder().decode(Receipt.self, from: Data(contentsOf: url))
            guard try hash(root.appendingPathComponent(receipt.file)) == receipt.digest else {
                throw AppError.message("Saved Photos export changed. Its original must be downloaded again before import.")
            }
            report.policies[receipt.file] = "keep"
            verified.append(url)
            } catch { report.failures.append(error.localizedDescription) }
        }
        report.pending = verified
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
