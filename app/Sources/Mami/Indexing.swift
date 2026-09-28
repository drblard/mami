import AppKit
import SwiftUI

/// The worker owns durable checkpoints. This object only controls it and renders
/// progress; neither file hashing nor inference runs on the UI thread.
@MainActor final class Indexing: ObservableObject {
    enum Lane: String {
        case search = "index"
        case previews = "preview"
        var title: String { self == .previews ? "Previews" : "AI search" }
    }
    static let shared = Indexing(lane: .search)
    static let previews = Indexing(lane: .previews)
    let lane: Lane
    private init(lane: Lane) { self.lane = lane }

    static func startAll() {
        previews.start()
        shared.start()
    }

    static func publishedImport() {
        CatalogUpdates.shared.notify()
        previews.wakeWork()
        shared.wakeWork()
    }
    struct Progress: Decodable {
        struct Counts: Decodable {
            let remaining: Int
            let completed: Int
            let failed: Int
        }
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
        let queue_counts: Counts?
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
    @Published private(set) var queueCounts: Progress.Counts?
    private var process: Process?
    private var input: FileHandle?
    private var output: FileHandle?
    private var buffer = Data()
    private var retries = 0
    private var quitting = false
    private var editorTimer: Timer?
    static func activeEditing(bundleID: String?, idleSeconds: Double) -> Bool {
        bundleID == "com.lemon.lvoverseas" && idleSeconds.isFinite && idleSeconds >= 0 && idleSeconds < 60
    }
    private func reportEditorActivity() {
        let idle = CGEventSource.secondsSinceLastEventType(.combinedSessionState, eventType: CGEventType(rawValue: UInt32.max)!)
        send(["action": "editor-activity", "active": Self.activeEditing(bundleID: NSWorkspace.shared.frontmostApplication?.bundleIdentifier, idleSeconds: idle)])
    }
    var active: Bool { running && !["Up to date", "Needs attention", "Scan needs attention", "Previews ready", "Preview needs attention", "Waiting for previews"].contains(phase) }
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
                               "--artifacts", Catalog.standard.artifacts.path, "--role", lane.rawValue]
            var environment = ProcessInfo.processInfo.environment
            environment["HF_HUB_OFFLINE"] = "1"
            environment["PYTHONUNBUFFERED"] = "1"
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            task.environment = environment
            let stdin = Pipe(), stdout = Pipe()
            task.standardInput = stdin
            task.standardOutput = stdout
            task.standardError = FileHandle.standardError
            // Utility QoS avoids macOS's severe background disk throttling;
            // the worker uses nice(10), bounded CPU threads and active-editor signals.
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
                    self.editorTimer?.invalidate(); self.editorTimer = nil
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
            if lane == .search {
                reportEditorActivity()
                editorTimer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
                    Task { @MainActor in self?.reportEditorActivity() }
                }
            }
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
                if let counts = progress.queue_counts { queueCounts = counts }
                if let message = progress.error { error = message }
                else if phase == "Up to date" || phase == "Previews ready" { error = nil }
                if progress.changed {
                    catalogGeneration += 1
                    CatalogUpdates.shared.notify()
                    if lane == .previews { Self.shared.wakeWork() }
                }
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
    func wakeWork() { if !running { start() }; send(["action": "work"]) }
    func retry() { error = nil; if !running { retries = 0; start() }; send(["action": "retry"]) }
    func stop() {
        quitting = true
        editorTimer?.invalidate(); editorTimer = nil
        send(["action": "stop"])
        try? input?.close()
        process?.terminate()
    }
}

struct IndexQueueSummary: View {
    @ObservedObject var indexing = Indexing.shared
    var body: some View {
        HStack(spacing: 8) {
            if let counts = indexing.queueCounts {
                Text("\(indexing.lane.title): \(counts.remaining) left · \(counts.completed) \(indexing.lane == .previews ? "ready" : "indexed")")
                if counts.failed > 0 { Text("\(counts.failed) need attention").foregroundStyle(.orange) }
            } else { Text("Counting indexing queue…") }
        }.monospacedDigit().lineLimit(1)
            .help("Files in the background indexing queue. Left includes the current file and failed jobs; completed imports and existing baseline indexes are tracked separately. The progress bar below shows work within the current file or scan.")
    }
}

struct IndexingBar: View {
    @ObservedObject var indexing = Indexing.shared
    private var detail: String {
        if let error = indexing.error { return error }
        if indexing.phase.contains("GPU"), let usage = indexing.gpuUtilization {
            return "Graphics activity: \(Int(usage))% · Transcription resumes after a quiet interval."
        }
        return ""
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack {
                Label("\(indexing.lane.title) · \(indexing.label)", systemImage: indexing.paused ? "pause.circle" : "arrow.triangle.2.circlepath")
                    .lineLimit(1)
                if !indexing.current.isEmpty { Text(indexing.current).lineLimit(1).truncationMode(.middle).foregroundStyle(.secondary) }
                Spacer()
                if indexing.total > 0 && indexing.active { Text("\(indexing.done) / \(indexing.total)").monospacedDigit().foregroundStyle(.secondary) }
                Button(indexing.paused ? "Resume" : "Pause") { indexing.togglePause() }.disabled(!indexing.running)
                if indexing.lane == .search { Button("Scan now") { indexing.scanNow() } }
            }.font(.caption).frame(height: 22)
             Group {
                 if indexing.total > 0 { ProgressView(value: Double(indexing.done), total: Double(max(indexing.total, indexing.done))) }
                 else { ProgressView().progressViewStyle(.linear) }
             }.frame(height: 4).opacity(indexing.active ? 1 : 0)
             // Keep one message slot allocated in every state. Error messages
             // take precedence over GPU details instead of adding another row.
             HStack {
                 Text(detail.isEmpty ? " " : detail)
                     .foregroundStyle(indexing.error == nil ? Color.secondary : Color.orange)
                     .lineLimit(1).truncationMode(.tail).help(detail)
                 Spacer(minLength: 0)
                 Button("Retry") { indexing.retry() }
                     .controlSize(.mini)
                     .opacity(indexing.error == nil ? 0 : 1)
                     .disabled(indexing.error == nil)
                     .accessibilityHidden(indexing.error == nil)
             }.font(.caption2).frame(height: 18)
        }.padding(.horizontal, 14).padding(.bottom, 6)
    }
}
