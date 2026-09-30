import AppKit
import SwiftUI
import MamiCore
import OSLog

@MainActor final class Importing: ObservableObject {
    static let shared = Importing()
    struct Progress: Decodable {
        let phase: String
        let current: String
        let done: Int
        let total: Int
        let copied: Int
        let duplicates: Int
        let failed: Int
        let skipped: Int
        let removed: Int?
        let paused: Bool
        let bytes_done: Int64
        let bytes_total: Int64
        let error: String?
        let ejection: String?
        let catalog_changed: Bool?
    }
    @Published var source: URL?
    @Published var device = "DJI-Pocket-4P"
    @Published var automaticDJI = UserDefaults.standard.object(forKey: "dji.automatic") as? Bool ?? true {
        didSet { UserDefaults.standard.set(automaticDJI, forKey: "dji.automatic") }
    }
    @Published var removeSource = UserDefaults.standard.bool(forKey: "dji.removeSource") {
        didSet { UserDefaults.standard.set(removeSource, forKey: "dji.removeSource") }
    }
    @Published var includeProxies = UserDefaults.standard.bool(forKey: "dji.includeProxies") {
        didSet { UserDefaults.standard.set(includeProxies, forKey: "dji.includeProxies") }
    }
    @Published var ejectAfter = UserDefaults.standard.object(forKey: "dji.ejectAfter") as? Bool ?? true {
        didSet { UserDefaults.standard.set(ejectAfter, forKey: "dji.ejectAfter") }
    }
    @Published private(set) var cameraStatus = "Waiting for DJI camera"
    @Published private(set) var cameraError: String?
    private var cameraTimer: Timer?
    private var cameraObservers: [NSObjectProtocol] = []
    private var connections = CameraConnections()
    private var manualCameraPending = false
    private let cameraLog = Logger(subsystem: "local.mami.prototype", category: "CameraOffload")
    @Published var policies: [String: String] = [:]
    @Published private(set) var sourceFiles: [String] = []
    @Published private(set) var listing = false
    @Published private(set) var photosTransfer = false
    @Published private(set) var running = false
    @Published private(set) var progress: Progress?
    @Published private(set) var error: String?
    private var process: Process?
    private var input: FileHandle?
    private var output: FileHandle?
    private var buffer = Data()
    private var lastCopyCount = 0
    private var photosDestination: URL?
    var removesAnySource: Bool { removeSource || policies.values.contains("remove") }
    var destination: URL {
        if let photosDestination { return photosDestination }
        if CommandLine.arguments.contains("--ui-test"), let path = ProcessInfo.processInfo.environment["MAMI_IMPORT_TEST_DESTINATION"] { return URL(fileURLWithPath: path) }
        return FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Media/Originals")
    }

    func startAutomatic() {
        guard cameraTimer == nil, !CommandLine.arguments.contains("--ui-test") else { return }
        cameraObservers.append(NSWorkspace.shared.notificationCenter.addObserver(forName: NSWorkspace.didUnmountNotification, object: nil, queue: .main) { [weak self] notification in
            guard let url = notification.userInfo?[NSWorkspace.volumeURLUserInfoKey] as? URL else { return }
            Task { @MainActor in self?.connections.disconnected(url) }
        })
        cameraObservers.append(NSWorkspace.shared.notificationCenter.addObserver(forName: NSWorkspace.didMountNotification, object: nil, queue: .main) { [weak self] notification in
            guard let url = notification.userInfo?[NSWorkspace.volumeURLUserInfoKey] as? URL else { return }
            Task { @MainActor in
                guard let self else { return }
                self.connections.disconnected(url) // A fresh mount invalidates a missed unmount signal.
                self.cameraLog.info("Volume mounted; checking for DJI")
                self.checkCamera()
            }
        })
        cameraTimer = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.checkCamera() }
        }
        checkCamera()
    }

    func retryCamera() {
        connections.retry()
        manualCameraPending = true
        checkCamera(manual: true)
    }

    private func checkCamera(manual: Bool = false) {
        let keys: Set<URLResourceKey> = [.volumeIsInternalKey, .volumeIsLocalKey]
        let volumes = FileManager.default.mountedVolumeURLs(includingResourceValuesForKeys: Array(keys), options: [.skipHiddenVolumes]) ?? []
        // Recognize this camera by its DJI database and DCIM layout, rather than
        // importing arbitrary removable drives or relying on a mutable disk name.
        let cameras = volumes.filter { url in
            guard let values = try? url.resourceValues(forKeys: keys), values.volumeIsInternal == false,
                  values.volumeIsLocal == true else { return false }
            return FileManager.default.fileExists(atPath: url.appendingPathComponent("MISC/PP-041.db").path)
                && FileManager.default.fileExists(atPath: url.appendingPathComponent("DCIM").path)
        }.sorted { $0.path < $1.path }
        if (manual || manualCameraPending) && cameras.isEmpty {
            manualCameraPending = false
            cameraStatus = "No DJI camera connected"
            return
        }
        if manualCameraPending && photosTransfer { cameraStatus = "Camera check queued — waiting for the current Photos transfer" }
        if (automaticDJI || manualCameraPending), connections.hasPending(cameras), running || listing || photosTransfer {
            let reason = photosTransfer ? "the Photos transfer" : listing ? "the file review" : "the current import"
            let status = "DJI connected — waiting for \(reason)"
            if cameraStatus != status { cameraLog.info("DJI detected; import deferred while busy") }
            cameraStatus = status
        }
        guard let camera = connections.next(cameras, enabled: automaticDJI || manualCameraPending, busy: running || listing || photosTransfer) else { return }
        manualCameraPending = false
        source = camera
        device = "DJI-Pocket-4P"
        policies = UserDefaults.standard.dictionary(forKey: "import-policies:" + camera.path) as? [String: String] ?? [:]
        sourceFiles = []
        cameraError = nil
        cameraStatus = "Starting DJI offload…"
        cameraLog.info("Starting detected DJI offload")
        start()
        if !running {
            cameraError = error; cameraStatus = "Offload needs attention — retry when ready"
            cameraLog.error("DJI worker did not start; explicit retry is available")
        }
    }

    func chooseSource() {
        guard !photosTransfer else { return }
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.prompt = "Choose source"
        panel.message = "Choose a mounted camera card or folder of original media."
        if panel.runModal() == .OK, let url = panel.url {
            source = url
            policies = UserDefaults.standard.dictionary(forKey: "import-policies:" + url.path) as? [String: String] ?? [:]
            sourceFiles = []; listing = true; error = nil
            Task {
                do {
                    let files = try await Task.detached(priority: .utility) {
                        var result: [String] = []
                        var failure: Error?
                        guard let entries = FileManager.default.enumerator(at: url, includingPropertiesForKeys: [.isRegularFileKey, .isSymbolicLinkKey], options: [.skipsHiddenFiles], errorHandler: { _, error in failure = error; return false }) else { throw AppError.message("Cannot list source") }
                        while let file = entries.nextObject() as? URL {
                            let info = try file.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey])
                            if info.isSymbolicLink == true { entries.skipDescendants(); continue }
                            if info.isRegularFile == true, ["mp4", "mov", "jpg", "jpeg", "png", "heic", "lrf"].contains(file.pathExtension.lowercased()) {
                                result.append(String(file.path.dropFirst(url.path.count + 1)))
                            }
                        }
                        if let failure { throw failure }
                        return result.sorted()
                    }.value
                    sourceFiles = files
                } catch { self.error = error.localizedDescription }
                listing = false
            }
        }
    }
    func setPolicy(_ value: String, for file: String) {
        policies[file] = value == "default" ? nil : value
        if let source { UserDefaults.standard.set(policies, forKey: "import-policies:" + source.path) }
    }
    func importPhotosFolder(_ folder: URL, destination: URL, receipts: [URL]) async throws {
        guard !running, !listing, !photosTransfer else { throw AppError.message("Camera import is busy. Photos will retry automatically.") }
        let previous = (source, device, policies, sourceFiles)
        photosTransfer = true
        photosDestination = destination
        defer { (source, device, policies, sourceFiles) = previous; photosTransfer = false; photosDestination = nil }
        source = folder; device = "iCloud"; policies = [:]; sourceFiles = []
        start(photos: true, receipts: receipts)
        while running { try await Task.sleep(for: .milliseconds(250)) }
        if let error { throw AppError.message(error) }
        guard let progress, progress.phase == "Import complete", progress.failed == 0 else {
            throw AppError.message("Photos exports are saved, but import has not completed. It will retry automatically.")
        }
    }
    func start(photos: Bool = false, receipts: [URL] = []) {
        guard !running, !listing, !photosTransfer || photos, let source else { return }
        do {
            let config = try Configuration.load()
            let task = Process(), stdin = Pipe(), stdout = Pipe()
            task.executableURL = config.python
            var environment = config.workerEnvironment
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            task.environment = environment
            task.arguments = [Bundle.main.resourceURL!.appendingPathComponent("import_media.py").path,
                              "--source", source.path, "--destination", destination.path, "--device", device,
                               "--catalog", Catalog.standard.database.path]
            if removeSource { task.arguments?.append("--remove-source") }
            if includeProxies && !photos { task.arguments?.append("--include-proxies") }
            if ejectAfter && !photos { task.arguments?.append("--eject-after") }
            if photos { task.arguments?.append("--direct-destination") }
            let policyDirectory = Catalog.standard.directory.appendingPathComponent("import-policies")
            try FileManager.default.createDirectory(at: policyDirectory, withIntermediateDirectories: true)
            let policyFile = policyDirectory.appendingPathComponent(UUID().uuidString + ".json")
            try JSONEncoder().encode(policies).write(to: policyFile, options: .atomic)
            task.arguments?.append(contentsOf: ["--policy-file", policyFile.path])
            if photos {
                try JSONEncoder().encode(receipts.map(\.path)).write(to: policyFile, options: .atomic)
                task.arguments = [Bundle.main.resourceURL!.appendingPathComponent("photos_batch.py").path,
                                  "--source", source.path, "--destination", destination.path,
                                  "--catalog", Catalog.standard.database.path, "--manifest", policyFile.path]
            }
            task.standardInput = stdin
            task.standardOutput = stdout
            task.standardError = AppDiagnostics.workerErrorOutput
            task.qualityOfService = .utility
            buffer = Data(); progress = nil; error = nil; lastCopyCount = 0
            try task.run()
            process = task; input = stdin.fileHandleForWriting; output = stdout.fileHandleForReading
            running = true
            // Consume all final progress before handling exit. A termination
            // callback can otherwise race the final pipe readability callback.
            Task.detached { [weak self] in
                while true {
                    let data = stdout.fileHandleForReading.availableData
                    if data.isEmpty { break }
                    await self?.receive(data)
                }
                task.waitUntilExit()
                await self?.finished(task.terminationStatus)
            }
        } catch { self.error = error.localizedDescription }
    }
    private func finished(_ status: Int32) {
        running = false
        input = nil; process = nil; output = nil
        if status != 0 { error = "Import stopped unexpectedly. Choose the same source and device folder to resume saved work." }
        if !photosTransfer {
            cameraError = error
            cameraStatus = error == nil ? (progress?.ejection ?? progress?.phase ?? "Offload finished") : "Offload needs attention — reconnect and retry"
        }
        if progress?.catalog_changed == nil { requestScan() }
        else if !CommandLine.arguments.contains("--ui-test") { Indexing.publishedImport() }
    }
    private func requestScan() { if !CommandLine.arguments.contains("--ui-test") { Indexing.shared.scanNow() } }
    private func receive(_ data: Data) {
        buffer.append(data)
        while let newline = buffer.firstIndex(of: 10) {
            let line = Data(buffer[..<newline])
            buffer.removeSubrange(...newline)
            do {
                let value = try JSONDecoder().decode(Progress.self, from: line)
                progress = value
                if !photosTransfer {
                    cameraStatus = value.ejection ?? (value.current.isEmpty ? value.phase : "\(value.phase) · \(value.current)")
                    if let message = value.error { cameraError = message }
                }
                if let message = value.error { error = message }
                if value.catalog_changed == true && !CommandLine.arguments.contains("--ui-test") {
                    Indexing.publishedImport()
                }
                if value.copied > lastCopyCount {
                    lastCopyCount = value.copied
                    if value.catalog_changed == nil { requestScan() }
                }
            } catch { self.error = "Could not read import progress: \(error.localizedDescription)" }
        }
    }
    private func send(_ action: String) {
        guard let input else { return }
        do {
            var data = try JSONSerialization.data(withJSONObject: ["action": action])
            data.append(10)
            try WorkerPipe.write(data, to: input)
        } catch { self.error = error.localizedDescription }
    }
    func togglePause() { send(progress?.paused == true ? "resume" : "pause") }
    func stop() { send("stop") }
    func shutdown() { stop(); try? input?.close(); process?.terminate() }
}

struct ImportSheet: View {
    @ObservedObject var importing = Importing.shared
    @ObservedObject var photos = PhotosImporting.shared
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Import media").font(.title2.bold())
            GroupBox("iCloud Photos — cable-free") {
                VStack(alignment: .leading, spacing: 8) {
                    Text(photos.status).font(.caption)
                    if photos.needsPhotosAccess { Button("Allow Photos access…") { photos.enable() }.disabled(photos.running) }
                    Text("Configure the destination, start date and automatic import in Settings (⌘,).")
                        .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    HStack {
                        SettingsLink { Text("iCloud import settings…") }
                        if photos.running { ProgressView().controlSize(.small) }
                    }
                    if let error = photos.error { Text(error).font(.caption).foregroundStyle(.orange) }
                }.frame(maxWidth: .infinity, alignment: .leading).padding(4)
            }
            Text("Copy originals from a camera card or folder into your library. Completed copies are verified and indexed automatically.")
                .foregroundStyle(.secondary)
            HStack {
                Text(importing.source?.path ?? "Choose a mounted card or media folder").lineLimit(2).truncationMode(.middle)
                Spacer()
                Button("Choose source…") { importing.chooseSource() }.disabled(importing.running)
            }
            HStack {
                TextField("Device folder", text: $importing.device).textFieldStyle(.roundedBorder)
                Menu("Presets") {
                    Button("DJI Pocket") { importing.device = "DJI-Pocket-4P" }
                    Button("Mami’s iPhone") { importing.device = "Mami-iPhone-16-Pro-Max" }
                }
            }.disabled(importing.running)
            Text("Saved to \(importing.destination.appendingPathComponent(importing.device).path)/year/date/")
                .font(.caption).lineLimit(2).truncationMode(.middle).textSelection(.enabled)
            SettingsLink { Text("Camera offload settings…") }
            Text("Each removal requires freshly matching SHA-256 hashes of both the source and the saved copy. This also applies to duplicates. Skipped files stay on the device.")
                .font(.caption).foregroundStyle(.secondary)
            if importing.listing { ProgressView("Listing source files…") }
            if !importing.sourceFiles.isEmpty {
                DisclosureGroup("Per-file exceptions (\(importing.sourceFiles.count) files)") {
                    ScrollView {
                        LazyVStack {
                            ForEach(importing.sourceFiles, id: \.self) { file in
                                HStack {
                                    Text(file + (file.lowercased().hasSuffix(".lrf") ? " · DJI proxy" : "")).font(.caption).lineLimit(1).truncationMode(.middle)
                                    Spacer()
                                    Picker("Action", selection: Binding(get: { importing.policies[file] ?? "default" }, set: { importing.setPolicy($0, for: file) })) {
                                        Text(importing.removeSource ? "Default: import & remove" : "Default: import & keep").tag("default")
                                        Text("Skip — leave untouched").tag("skip")
                                        Text("Import & keep").tag("keep")
                                        Text("Import & remove").tag("remove")
                                    }.labelsHidden().frame(width: 220)
                                        .disabled(file.lowercased().hasSuffix(".lrf") && !importing.includeProxies)
                                }
                            }
                        }
                    }.frame(height: 180)
                }.disabled(importing.running)
            }
            if let value = importing.progress {
                Divider()
                Text(value.paused ? "Paused — saved copy will resume" : value.phase).font(.headline)
                if !value.current.isEmpty { Text(value.current).lineLimit(1).truncationMode(.middle) }
                if importing.running {
                    if value.bytes_total > 0 { ProgressView(value: Double(value.bytes_done), total: Double(max(value.bytes_total, value.bytes_done))) }
                    else if value.total > 0 { ProgressView(value: Double(value.done), total: Double(value.total)) }
                    else { ProgressView().controlSize(.small) }
                }
                Text("\(value.done) of \(value.total) files checked · \(value.copied) copied · \(value.duplicates) already imported · \(value.failed) need attention · \(value.skipped) unsupported/proxy files skipped")
                    .font(.caption).foregroundStyle(.secondary)
                if let removed = value.removed, removed > 0 { Text("\(removed) verified files removed from source").font(.caption) }
                if let ejection = value.ejection { Text(ejection).font(.caption) }
            }
            if let error = importing.error { Text(error).font(.caption).foregroundStyle(.orange).textSelection(.enabled) }
            HStack {
                Button(importing.running ? "Keep importing in background" : "Close") { dismiss() }
                Spacer()
                if importing.running {
                    Button(importing.progress?.paused == true ? "Resume" : "Pause") { importing.togglePause() }
                    Button("Stop") { importing.stop() }
                } else {
                    Button(importing.removesAnySource ? "Import, verify & remove" : (importing.progress == nil ? "Import & verify" : "Import / resume")) { importing.start() }
                        .buttonStyle(.borderedProminent).disabled(importing.listing || importing.source == nil || importing.device.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            }
            Text("To resume after closing Mami, select the same source and device folder again.").font(.caption2).foregroundStyle(.secondary)
        }.padding(24).frame(width: 600)
            .background(Color(red: 0.08, green: 0.09, blue: 0.11)).preferredColorScheme(.dark)
    }
}
