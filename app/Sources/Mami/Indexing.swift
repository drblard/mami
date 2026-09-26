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
        let gpu_utilization: Double?
    }
    @Published private(set) var phase = "Automatic scanning starts with the library"
    @Published private(set) var current = ""
    @Published private(set) var done = 0
    @Published private(set) var total = 0
    @Published private(set) var paused = false
    @Published private(set) var waiting = false
    @Published private(set) var error: String?
    @Published private(set) var running = false
    @Published private(set) var catalogGeneration = 0
    @Published private(set) var gpuUtilization: Double?
    private var process: Process?
    private var input: FileHandle?
    private var output: FileHandle?
    private var buffer = Data()
    private var retries = 0
    private var quitting = false
    var active: Bool { running && !["Up to date", "Needs attention", "Scan needs attention"].contains(phase) }
    var label: String {
        if paused { return waiting || !active ? "Paused — progress saved" : "Pausing after current step…" }
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
            // Utility QoS avoids macOS's severe background disk throttling;
            // the worker still uses nice(10), one CPU thread and GPU-load checks.
            task.qualityOfService = .utility
            buffer = Data()
            stdout.fileHandleForReading.readabilityHandler = { [weak self, weak task] handle in
                let data = handle.availableData
                if data.isEmpty { handle.readabilityHandler = nil; return }
                Task { @MainActor in
                    guard let self, self.process === task else { return }
                    self.receive(data)
                }
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
                waiting = progress.waiting ?? false
                gpuUtilization = progress.gpu_utilization
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
            try WorkerPipe.write(data, to: input)
        } catch { self.error = "Could not control indexing: \(error.localizedDescription)" }
    }
    func togglePause() { send(["action": paused ? "resume" : "pause"]) }
    func scanNow() { if !running { retries = 0; start() }; send(["action": "scan"]) }
    func retry() { error = nil; if !running { retries = 0; start() }; send(["action": "retry"]) }
    func stop() {
        quitting = true
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
                    .lineLimit(1)
                if !indexing.current.isEmpty { Text(indexing.current).lineLimit(1).truncationMode(.middle).foregroundStyle(.secondary) }
                Spacer()
                if indexing.total > 0 && indexing.active { Text("\(indexing.done) / \(indexing.total)").monospacedDigit().foregroundStyle(.secondary) }
                Button(indexing.paused ? "Resume" : "Pause") { indexing.togglePause() }.disabled(!indexing.running)
                Button("Scan now") { indexing.scanNow() }
            }.font(.caption)
             Group {
                 if indexing.total > 0 { ProgressView(value: Double(indexing.done), total: Double(max(indexing.total, indexing.done))) }
                 else { ProgressView().progressViewStyle(.linear) }
             }.frame(height: 4).opacity(indexing.active ? 1 : 0)
            if indexing.phase.contains("GPU"), let usage = indexing.gpuUtilization {
                Text("Graphics activity: \(Int(usage))% · Transcription resumes after a quiet interval. CapCut can stay open.")
                    .font(.caption2).foregroundStyle(.secondary)
            }
            if let error = indexing.error {
                HStack {
                    Text(error).foregroundStyle(.orange).lineLimit(1).help(error)
                    Button("Retry") { indexing.retry() }
                }.font(.caption)
            }
        }.frame(height: 64, alignment: .top).padding(.horizontal, 14).padding(.bottom, 10)
    }
}
