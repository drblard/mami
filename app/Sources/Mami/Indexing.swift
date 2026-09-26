import AppKit
import SwiftUI

/// The worker owns durable checkpoints. This object only controls it and renders
/// progress; neither file hashing nor inference runs on the UI thread.
@MainActor final class Indexing: ObservableObject {
    static let shared = Indexing()
    struct Progress: Decodable {
        let phase: String
        let done: Int
        let total: Int
        let current: String
        let paused: Bool
        let busy: Bool
        let waiting: Bool?
        let changed: Bool
        let error: String?
    }
    @Published private(set) var phase = "Automatic scanning starts with the library"
    @Published private(set) var current = ""
    @Published private(set) var done = 0
    @Published private(set) var total = 0
    @Published private(set) var paused = false
    @Published private(set) var waiting = false
    @Published private(set) var foregroundBusy = false
    @Published private(set) var error: String?
    @Published private(set) var running = false
    @Published private(set) var catalogGeneration = 0
    @Published private(set) var capCutRunning = false
    private var process: Process?
    private var input: FileHandle?
    private var output: FileHandle?
    private var buffer = Data()
    private var searchBusy = false
    private var previews = Set<UUID>()
    private var lastCapCutSeen = Date.distantPast
    private var editorTask: Task<Void, Never>?
    private var retries = 0
    private var quitting = false
    var active: Bool { running && !["Up to date", "Needs attention", "Scan needs attention"].contains(phase) }
    var label: String {
        if paused { return waiting || !active ? "Paused — progress saved" : "Pausing after current step…" }
        if foregroundBusy && active { return "Yielding to preview/search" }
        return phase
    }

    func start() {
        guard process == nil, !quitting else { return }
        do {
            let config = try Configuration.load()
            let task = Process()
            task.executableURL = config.python
            task.arguments = [(Bundle.main.resourceURL!.appendingPathComponent("index_worker.py")).path,
                              "--database", Catalog.standard.database.path,
                              "--root", ProcessInfo.processInfo.environment["MAMI_MEDIA_ROOT"] ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Media/Originals").path,
                              "--artifacts", FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("mami-lab/index-artifacts").path]
            var environment = ProcessInfo.processInfo.environment
            environment["HF_HUB_OFFLINE"] = "1"
            environment["PYTHONUNBUFFERED"] = "1"
            task.environment = environment
            let stdin = Pipe(), stdout = Pipe()
            task.standardInput = stdin
            task.standardOutput = stdout
            task.standardError = FileHandle.standardError
            task.qualityOfService = .background
            buffer = Data()
            stdout.fileHandleForReading.readabilityHandler = { [weak self] handle in
                let data = handle.availableData
                if data.isEmpty { handle.readabilityHandler = nil; return }
                Task { @MainActor in self?.receive(data) }
            }
            task.terminationHandler = { [weak self] ended in
                Task { @MainActor in
                    guard let self, self.process === ended else { return }
                    self.running = false
                    self.output?.readabilityHandler = nil
                    self.process = nil
                    self.input = nil
                    if !self.quitting {
                        self.error = "Background worker stopped; completed checkpoints are saved."
                        if ended.terminationStatus != 0 && self.retries < 3 {
                            self.retries += 1
                            try? await Task.sleep(for: .seconds(5))
                            self.start()
                        }
                    }
                }
            }
            try task.run()
            process = task
            input = stdin.fileHandleForWriting
            output = stdout.fileHandleForReading
            running = true
            error = nil
            refreshEditors()
            send(["action": "gpu-busy", "value": capCutRunning])
            send(["action": "busy", "value": searchBusy || !previews.isEmpty])
            editorTask?.cancel()
            editorTask = Task {
                while !Task.isCancelled {
                    do { try await Task.sleep(for: .seconds(3)) } catch { return }
                    refreshEditors()
                }
            }
        } catch { self.error = "Could not start background indexing: \(error.localizedDescription)" }
    }

    private func receive(_ data: Data) {
        buffer.append(data)
        while let newline = buffer.firstIndex(of: 10) {
            let line = Data(buffer[..<newline])
            buffer.removeSubrange(...newline)
            do {
                let progress = try JSONDecoder().decode(Progress.self, from: line)
                phase = progress.phase; done = progress.done; total = progress.total
                current = progress.current; paused = progress.paused
                waiting = progress.waiting ?? false; foregroundBusy = progress.busy
                if let message = progress.error { error = message }
                else if phase == "Up to date" { error = nil }
                if progress.changed { catalogGeneration += 1 }
            } catch { self.error = "Could not read indexing progress: \(error.localizedDescription)" }
        }
    }

    private func send(_ value: [String: Any]) {
        guard let input else { return }
        do {
            var data = try JSONSerialization.data(withJSONObject: value)
            data.append(10)
            try input.write(contentsOf: data)
        } catch { self.error = "Could not control indexing: \(error.localizedDescription)" }
    }
    func togglePause() { send(["action": paused ? "resume" : "pause"]) }
    func scanNow() { if !running { retries = 0; start() }; send(["action": "scan"]) }
    func retry() { error = nil; if !running { retries = 0; start() }; send(["action": "retry"]) }
    func setSearchBusy(_ value: Bool) { searchBusy = value; send(["action": "busy", "value": searchBusy || !previews.isEmpty]) }
    func beginPreview(_ id: UUID) { previews.insert(id); send(["action": "busy", "value": true]) }
    func endPreview(_ id: UUID) { previews.remove(id); send(["action": "busy", "value": searchBusy || !previews.isEmpty]) }
    func refreshEditors() {
        let present = NSWorkspace.shared.runningApplications.contains {
            ($0.localizedName ?? "").localizedCaseInsensitiveContains("CapCut") || ["com.lemon.lvoverseas", "com.lemon.lv"].contains($0.bundleIdentifier ?? "")
        }
        if present { lastCapCutSeen = Date() }
        let busy = present || Date().timeIntervalSince(lastCapCutSeen) < 15
        if busy != capCutRunning || process != nil && editorTask == nil {
            capCutRunning = busy
            send(["action": "gpu-busy", "value": busy])
        }
    }
    func stop() {
        quitting = true
        editorTask?.cancel()
        send(["action": "stop"])
        try? input?.close()
        process?.terminate()
    }
}

struct IndexingBar: View {
    @ObservedObject var indexing = Indexing.shared
    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack {
                Label(indexing.label, systemImage: indexing.paused ? "pause.circle" : "arrow.triangle.2.circlepath")
                if !indexing.current.isEmpty { Text(indexing.current).lineLimit(1).truncationMode(.middle).foregroundStyle(.secondary) }
                Spacer()
                if indexing.total > 0 && indexing.active { Text("\(indexing.done) / \(indexing.total)").monospacedDigit().foregroundStyle(.secondary) }
                Button(indexing.paused ? "Resume" : "Pause") { indexing.togglePause() }.disabled(!indexing.running)
                Button("Scan now") { indexing.scanNow() }
            }.font(.caption)
            if indexing.active {
                if indexing.total > 0 { ProgressView(value: Double(indexing.done), total: Double(max(indexing.total, indexing.done))) }
                else { ProgressView().controlSize(.small) }
            }
            if indexing.capCutRunning { Text("GPU transcription waits while CapCut is running. CPU indexing remains low priority.").font(.caption2).foregroundStyle(.secondary) }
            if let error = indexing.error {
                HStack {
                    Text(error).foregroundStyle(.orange).lineLimit(2)
                    Button("Retry") { indexing.retry() }
                }.font(.caption)
            }
        }.padding(.horizontal, 14).padding(.bottom, 10)
    }
}
