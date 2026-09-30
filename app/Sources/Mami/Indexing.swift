import AppKit
import SwiftUI
import MamiCore

/// The worker owns durable checkpoints. This object only controls it and renders
/// progress; neither file hashing nor inference runs on the UI thread.
/// Bounded diagnostic trail of lane lifecycle events (launch, exit, dropped or
/// unreadable messages, failed commands) for problems seen only in the live app.
enum LaneLog {
    static let limit = 1024 * 1024
    static var file: URL { AppPaths().caches.appendingPathComponent("lane-events.log") }
    static func record(_ lane: String, _ event: String) {
        let line = "\(ISO8601DateFormatter().string(from: Date())) \(lane) \(event)\n"
        let url = file
        try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        if let size = (try? FileManager.default.attributesOfItem(atPath: url.path)[.size]) as? Int, size >= limit {
            try? FileManager.default.removeItem(at: url.appendingPathExtension("1"))
            try? FileManager.default.moveItem(at: url, to: url.appendingPathExtension("1"))
        }
        if let handle = try? FileHandle(forWritingTo: url) {
            defer { try? handle.close() }
            _ = try? handle.seekToEnd(); try? handle.write(contentsOf: Data(line.utf8))
        } else { try? Data(line.utf8).write(to: url) }
    }
}

@MainActor final class Indexing: ObservableObject {
    enum Lane: String, Sendable {
        case search = "index"
        case previews = "preview"
        case faces = "faces"
        var title: String {
            switch self {
            case .previews: return "Previews"
            case .search: return "AI search"
            case .faces: return "Faces"
            }
        }
        /// Lanes that run inference yield to active CapCut use before starting.
        var yieldsToEditor: Bool { self != .previews }
    }
    static let shared = Indexing(lane: .search)
    static let previews = Indexing(lane: .previews)
    static let faces = Indexing(lane: .faces)
    static var all: [Indexing] { [previews, shared, faces] }
    /// Routine library checks: shown only when the user asked for them. Work they
    /// discover (new or changed media) shows progress as usual.
    static let backgroundPhases: Set<String> = ["Discovering files", "Checking media"]
    /// Phases in which a worker has nothing to do; it is released after a grace period.
    static let idlePhases: Set<String> = ["Up to date", "Needs attention", "Scan needs attention", "Previews ready", "Preview needs attention",
                                          "Waiting for previews", "Faces ready", "Faces need attention", "Waiting for previews and AI search",
                                          "Face model not installed"]
    let lane: Lane
    private init(lane: Lane) { self.lane = lane }

    static func startAll() {
        previews.schedule()
        shared.schedule()
        faces.schedule()
        previews.wakeWork()
        shared.start(force: true)
        faces.start()
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
    /// Set by Scan now until the worker reports an idle phase.
    @Published private(set) var userRequestedScan = false
    private var process: Process?
    private var input: FileHandle?
    private var output: FileHandle?
    private var buffer = Data()
    private var retries = 0
    private var quitting = false
    private var editorTimer: Timer?
    private var wakeTimer: Timer?
    private var idleTask: Task<Void, Never>?
    private var checking = false
    private var retiring = false
    /// Commands that arrived after a retiring worker's input was closed; they are
    /// replayed to a fresh worker once the old one has exited.
    private var deferredActions: [[String: Any]] = []
    private var lastToken: String?
    private var workPending = false
    private var lastScan = Date.distantPast
    private static let workerIdleGrace: Duration = .seconds(15)
    private static let discoveryInterval: TimeInterval = 300
    static func activeEditing(bundleID: String?, idleSeconds: Double) -> Bool {
        bundleID == "com.lemon.lvoverseas" && idleSeconds.isFinite && idleSeconds >= 0 && idleSeconds < 60
    }
    private func reportEditorActivity() {
        let idle = CGEventSource.secondsSinceLastEventType(.combinedSessionState, eventType: CGEventType(rawValue: UInt32.max)!)
        send(["action": "editor-activity", "active": Self.activeEditing(bundleID: NSWorkspace.shared.frontmostApplication?.bundleIdentifier, idleSeconds: idle)])
    }
    var active: Bool { running && !Self.idlePhases.contains(phase) }
    var label: String {
        if paused { return waiting || !active ? "Paused — progress saved" : "Pausing after current step…" }
        return phase
    }

    private func schedule() {
        guard wakeTimer == nil else { return }
        wakeTimer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.start() }
        }
    }

    func start(force: Bool = false) {
        guard process == nil, !quitting, !checking else { return }
        let idle = CGEventSource.secondsSinceLastEventType(.combinedSessionState, eventType: CGEventType(rawValue: UInt32.max)!)
        if lane.yieldsToEditor && Self.activeEditing(bundleID: NSWorkspace.shared.frontmostApplication?.bundleIdentifier, idleSeconds: idle) {
            phase = "Waiting while CapCut is actively used"
            return
        }
        if lane == .faces && !FaceIndex.modelInstalled {
            phase = "Face model not installed"
            error = "Download the face model in Settings → Storage and local models."
            return
        }
        checking = true
        let lane = lane, token = lastToken
        Task {
            defer { checking = false }
            do {
                if let state = try await Task.detached(priority: .utility, operation: {
                    lane == .faces ? try FaceIndex.backgroundWork(catalog: .standard, previousToken: token)
                        : try BackgroundWork.read(catalog: .standard, preview: lane == .previews, previousToken: token)
                }).value {
                    lastToken = state.token; workPending = state.pending; paused = state.paused
                    queueCounts = Progress.Counts(remaining: state.remaining, completed: state.completed, failed: state.failed)
                }
                guard !quitting, process == nil else { return }
                let discoveryDue = lane == .search && Date().timeIntervalSince(lastScan) >= Self.discoveryInterval
                if force || workPending || (!paused && discoveryDue) { launch() }
                else if error == nil { phase = lane == .previews ? "Previews ready" : lane == .faces ? "Faces ready" : "Up to date" }
            } catch {
                if force { launch() }
                else { self.error = "Could not check background work: \(error.localizedDescription)" }
            }
        }
    }

    private func launch() {
        guard process == nil, !quitting else { return }
        do {
            let config = try Configuration.load()
            let task = Process()
            task.executableURL = config.python
            if lane == .faces {
                task.arguments = [(Bundle.main.resourceURL!.appendingPathComponent("face_worker.py")).path,
                                  "--catalog", Catalog.standard.database.path, "--faces", Catalog.standard.facesDatabase.path,
                                  "--models", FaceIndex.modelDirectory.path, "--image-helper", Bundle.main.executableURL!.path]
            } else {
                task.arguments = [(Bundle.main.resourceURL!.appendingPathComponent("index_worker.py")).path,
                                  "--database", Catalog.standard.database.path,
                                  "--root", ProcessInfo.processInfo.environment["MAMI_MEDIA_ROOT"] ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Media/Originals").path,
                                   "--artifacts", Catalog.standard.artifacts.path, "--role", lane.rawValue]
            }
            var environment = config.workerEnvironment
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
                    guard let self else { return }
                    guard self.process === task else {
                        LaneLog.record(self.lane.rawValue, "dropped \(data.count) bytes from pid \(task?.processIdentifier ?? -1); current pid \(self.process?.processIdentifier ?? -1)")
                        return
                    }
                    self.receive(data)
                }
            }
            task.terminationHandler = { [weak self] ended in
                Task { @MainActor in
                    guard let self else { return }
                    LaneLog.record(self.lane.rawValue, "exit pid \(ended.processIdentifier) status \(ended.terminationStatus) current \(self.process?.processIdentifier ?? -1) retiring \(self.retiring)")
                    guard self.process === ended else { return }
                    self.running = false
                    self.editorTimer?.invalidate(); self.editorTimer = nil
                    self.output?.readabilityHandler = nil
                    self.process = nil
                    self.input = nil
                    self.idleTask?.cancel(); self.idleTask = nil
                    self.lastToken = nil
                    if !self.quitting {
                        if self.retiring {
                            self.retiring = false
                            let actions = self.deferredActions
                            self.deferredActions = []
                            if !actions.isEmpty {
                                self.launch()
                                actions.forEach { self.send($0) }
                            }
                            return
                        }
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
            LaneLog.record(lane.rawValue, "launch pid \(task.processIdentifier)")
            retiring = false
            if lane == .search { lastScan = Date() }
            process = task
            input = stdin.fileHandleForWriting
            output = stdout.fileHandleForReading
            running = true
            if lane.yieldsToEditor {
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
                else if ["Up to date", "Previews ready", "Faces ready"].contains(phase) {
                    // A worker that reaches a healthy idle state earns back its restart budget.
                    error = nil; retries = 0
                }
                if progress.changed {
                    catalogGeneration += 1
                    // Face results are a separate generated store; the media catalog is unchanged.
                    if lane != .faces {
                        CatalogUpdates.shared.notify()
                        if lane == .previews { Self.shared.wakeWork() }
                        else { Self.previews.wakeWork() }
                        Self.faces.wakeWork()
                    }
                }
                let idle = paused || waiting || Self.idlePhases.contains(phase)
                if Self.idlePhases.contains(phase) { userRequestedScan = false }
                if idle && idleTask == nil {
                    let currentProcess = process
                    idleTask = Task { [weak self] in
                        do { try await Task.sleep(for: Self.workerIdleGrace) } catch { return }
                        guard let self, self.process === currentProcess else { return }
                        LaneLog.record(self.lane.rawValue, "retire idle pid \(currentProcess?.processIdentifier ?? -1)")
                        self.send(["action": "stop"])
                        self.retiring = true
                        try? self.input?.close()
                        do { try await Task.sleep(for: .seconds(2)) } catch { return }
                        if let currentProcess, currentProcess.isRunning { currentProcess.terminate() }
                    }
                } else if !idle { idleTask?.cancel(); idleTask = nil }
            } catch {
                LaneLog.record(lane.rawValue, "unreadable progress (\(line.count) bytes): \(String(decoding: line.prefix(200), as: UTF8.self))")
                self.error = "Could not read indexing progress: \(error.localizedDescription)"
            }
        }
    }

    private func send(_ value: [String: Any]) {
        if retiring {
            if value["action"] as? String != "stop" { deferredActions.append(value) }
            return
        }
        guard let input else { return }
        do {
            var data = try JSONSerialization.data(withJSONObject: value)
            data.append(10)
            try WorkerPipe.write(data, to: input)
        } catch {
            LaneLog.record(lane.rawValue, "command \(value["action"] ?? "?") failed: \(error.localizedDescription)")
            self.error = "Could not control indexing: \(error.localizedDescription)"
        }
    }
    func togglePause() {
        if !running { launch() }
        send(["action": paused ? "resume" : "pause"])
    }
    func scanNow() { userRequestedScan = true; if !running { retries = 0; launch() }; send(["action": "scan"]) }
    /// True while the lane only performs a routine check nobody asked to watch.
    var quietlyChecking: Bool { Self.backgroundPhases.contains(phase) && !userRequestedScan }
    func wakeWork() { if !running { start() }; send(["action": "work"]) }
    func retry() { error = nil; if !running { retries = 0; launch() }; send(["action": "retry"]) }
    /// Regroup faces and refresh suggestions after the user's people edits.
    func recomputeFaces() {
        guard lane == .faces, FaceIndex.modelInstalled else { return }
        if !running { retries = 0; launch() }
        send(["action": "recompute"])
    }
    func stop() {
        quitting = true
        wakeTimer?.invalidate(); wakeTimer = nil
        idleTask?.cancel(); idleTask = nil
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
                Text("\(indexing.lane.title): \(counts.remaining) left · \(counts.completed) \(indexing.lane == .previews ? "ready" : indexing.lane == .faces ? "scanned" : "indexed")")
                if counts.failed > 0 { Text("\(counts.failed) need attention").foregroundStyle(.orange) }
            } else { Text("Counting indexing queue…") }
        }.monospacedDigit().lineLimit(1)
            .help("Files in the background indexing queue. Left includes the current file and failed jobs; completed imports and existing baseline indexes are tracked separately. The progress bar below shows work within the current file or scan.")
    }
}

struct IndexingBar: View {
    @ObservedObject var indexing = Indexing.shared
    @ViewState private var shown = true
    private struct ProgressKey: Hashable {
        let phase: String, current: String, done: Int, total: Int
        let running: Bool, paused: Bool, error: String?, failed: Int
    }
    private var progressKey: ProgressKey {
        ProgressKey(phase: indexing.phase, current: indexing.current, done: indexing.done, total: indexing.total,
                    running: indexing.running, paused: indexing.paused, error: indexing.error, failed: indexing.queueCounts?.failed ?? 0)
    }
    private var failed: Int { indexing.queueCounts?.failed ?? 0 }
    /// Failed items keep the row (and its Retry) visible even when no worker is running.
    private var needsAttention: Bool { indexing.paused || indexing.error != nil || failed > 0 }
    private var detail: String {
        if let error = indexing.error { return error }
        if failed > 0 { return "\(failed) file\(failed == 1 ? "" : "s") need attention. Retry keeps completed work." }
        if indexing.phase.contains("GPU"), let usage = indexing.gpuUtilization {
            return "Graphics activity: \(Int(usage))% · Transcription resumes after a quiet interval."
        }
        return ""
    }
    var body: some View {
        // One row per lane: status, then the current file or a message, then controls.
        HStack(spacing: 8) {
            Label("\(indexing.lane.title) · \(indexing.label)", systemImage: indexing.paused ? "pause.circle" : "arrow.triangle.2.circlepath")
                .lineLimit(1).layoutPriority(1)
            if !detail.isEmpty {
                Text(detail).foregroundStyle(indexing.error == nil && failed == 0 ? Color.secondary : Color.orange)
                    .lineLimit(1).truncationMode(.tail).help(detail)
            } else if !indexing.current.isEmpty {
                Text(indexing.current).lineLimit(1).truncationMode(.middle).foregroundStyle(.secondary)
            }
            Spacer(minLength: 8)
            if indexing.total > 0 && indexing.active { Text("\(indexing.done) / \(indexing.total)").monospacedDigit().foregroundStyle(.secondary) }
            Group {
                if indexing.total > 0 { ProgressView(value: Double(indexing.done), total: Double(max(indexing.total, indexing.done))) }
                else { ProgressView().progressViewStyle(.linear) }
            }.frame(width: 120).opacity(indexing.active ? 1 : 0)
            if indexing.error != nil || failed > 0 { Button("Retry") { indexing.retry() } }
            Button(indexing.paused ? "Resume" : "Pause") { indexing.togglePause() }.disabled(indexing.queueCounts == nil)
            if indexing.lane == .search { Button("Scan now") { indexing.scanNow() } }
        }
        .font(.caption).controlSize(.small).padding(.horizontal, 14)
        // Collapse rather than remove the row so this view keeps observing progress.
        .frame(height: shown ? 26 : 0).clipped()
        .opacity(shown ? 1 : 0).allowsHitTesting(shown).accessibilityHidden(!shown)
        .animation(.easeInOut(duration: 0.25), value: shown)
        .task(id: progressKey) {
            // Routine checks never move the screen; only real work or problems do.
            if indexing.quietlyChecking { shown = needsAttention; return }
            shown = true
            guard !indexing.active, !needsAttention else { return }
            do { try await Task.sleep(for: ProgressVisibility.idleHideDelay) } catch { return }
            shown = ProgressVisibility.isVisible(active: indexing.active, needsAttention: needsAttention,
                                                 sinceLastProgress: ProgressVisibility.idleHideDelay)
        }
    }
}
