import AppKit
import Foundation
import MamiCore
import Darwin

/// Owns derived-index writers independently of interactive search requests.
@MainActor final class SearchMaintenance: ObservableObject {
    static let shared = SearchMaintenance()
    @Published private(set) var status = ""
    @Published private(set) var error: String?
    private var workers: [String: Process] = [:]
    private var inputs: [String: FileHandle] = [:]
    private var activityTimer: Timer?
    private var quitting = false
    private var failures: [String: String] = [:]
    private static let shutdownGrace: Duration = .seconds(2)
    private static let exitGrace: Duration = .seconds(5)
    /// Idle limit per read; a worker that has exited and stays silent this long is done.
    nonisolated private static let readTimeout: Duration = .seconds(65)
    private var configuration: Configuration?
    private var checking = false
    private var lastVectorAttempt = Date.distantPast
    private var retryAfter: [String: Date] = [:]
    var activeWorkerCount: Int { workers.values.filter(\.isRunning).count }

    private func setFailure(_ message: String?, for worker: String) {
        failures[worker] = message
        error = failures.isEmpty ? nil : failures.keys.sorted().map { "\($0): \(failures[$0]!)" }.joined(separator: " · ")
    }

    func start(_ configuration: Configuration) throws {
        guard activityTimer == nil, configuration.searchProjection != nil, configuration.packedIndex != nil else { return }
        quitting = false
        self.configuration = configuration
        activityTimer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.poll() }
        }
        poll()
    }

    private func poll() {
        guard !quitting, !checking, let configuration,
              let projection = configuration.searchProjection, let vectors = configuration.packedIndex else { return }
        reportActivity()
        checking = true
        let editorActive = Self.editorActive()
        Task {
            defer { checking = false }
            do {
                let needed = try await Task.detached(priority: .utility) {
                    try MaintenanceWork.read(catalog: .standard, projection: URL(fileURLWithPath: projection), vectors: URL(fileURLWithPath: vectors))
                }.value
                guard !quitting else { return }
                setFailure(nil, for: "scheduler")
                @MainActor func available(_ name: String) -> Bool { workers[name] == nil && (retryAfter[name] ?? .distantPast) <= Date() }
                if needed.projection && available("projection") {
                    var arguments = ["--catalog", Catalog.standard.database.path, "--output", projection, "--vector-root", vectors, "--once"]
                    if FileManager.default.fileExists(atPath: configuration.index.path) { arguments += ["--index", configuration.index.path] }
                    if let speech = configuration.speech, FileManager.default.fileExists(atPath: speech) { arguments += ["--speech", speech] }
                    try launch(name: "projection", script: "search_sync.py", configuration: configuration, arguments: arguments)
                }
                if !editorActive && !needed.projection && needed.vectors && available("vectors") && Date().timeIntervalSince(lastVectorAttempt) >= 60 {
                    let root = URL(fileURLWithPath: vectors)
                    if FileManager.default.fileExists(atPath: root.appendingPathComponent("owner.json").path) ||
                        root == AppPaths().search.appendingPathComponent("Vectors") {
                        lastVectorAttempt = Date()
                        try launch(name: "vectors", script: "vector_sync.py", configuration: configuration,
                                   arguments: ["--projection", projection, "--output", vectors, "--once", "--scheduled"])
                    }
                }
                if !editorActive && configuration.packPreviews && needed.previews && available("preview-cache") {
                    try launch(name: "preview-cache", script: "preview_cache_worker.py", configuration: configuration,
                               arguments: ["--database", Catalog.standard.database.path, "--artifacts", Catalog.standard.artifacts.path, "--projection", projection, "--once"])
                }
            } catch { setFailure("Could not schedule maintenance: \(error.localizedDescription)", for: "scheduler") }
        }
    }

    private func launch(name: String, script: String, configuration: Configuration, arguments: [String]) throws {
        let process = Process(), input = Pipe(), output = Pipe()
        guard let resources = Bundle.main.resourceURL else { throw AppError.message("Worker resources unavailable") }
        process.executableURL = configuration.python
        process.arguments = ["-B", resources.appendingPathComponent(script).path] + arguments
        var environment = configuration.workerEnvironment
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["OMP_NUM_THREADS"] = "4"
        environment["OPENBLAS_NUM_THREADS"] = "4"
        process.environment = environment
        process.standardInput = input
        process.standardOutput = output
        process.standardError = AppDiagnostics.workerErrorOutput
        process.qualityOfService = .utility
        try process.run()
        watch(name, process: process, input: input, output: output)
    }

    private func watch(_ name: String, process: Process, input: Pipe, output: Pipe) {
        workers[name] = process
        inputs[name] = input.fileHandleForWriting
        Task.detached { [weak self] in
            var reader = LineReader(handle: output.fileHandleForReading, maximumLineBytes: 1024 * 1024)
            do {
                // Drain to EOF: the final lines (errors, "changed") often follow the exit.
                while true {
                    do {
                        let line = try reader.readLine(timeout: Self.readTimeout)
                        await self?.receive(line, from: name)
                    } catch WorkerTransportError.timedOut where !process.isRunning { break }
                    catch WorkerTransportError.timedOut { continue }
                }
            } catch WorkerTransportError.closed { }
            catch { await self?.report(error, worker: name) }
            // Output EOF usually precedes the observed exit. A worker that closed
            // its output but keeps running is reaped rather than left holding locks.
            _ = await ProcessExit.reap(process, grace: Self.exitGrace)
            await self?.finished(name, process: process)
        }
    }

    private func receive(_ data: Data, from name: String) {
        do {
            guard let event = try JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
            if let message = event["error"] as? String { setFailure(message, for: name) }
            if event["ready"] as? Bool == true || event["stage"] as? String == "ready" { status = "Search index up to date"; setFailure(nil, for: name) }
            if event["stage"] as? String == "building" { status = "Updating visual search in the background" }
            if event["stage"] as? String == "deferred" { status = "Visual maintenance waits for active editing" }
            if event["changed"] as? Bool == true { CatalogUpdates.shared.notify() }
        } catch { setFailure("Could not read progress: \(error.localizedDescription)", for: name) }
    }

    private func report(_ failure: Error, worker: String) {
        if !quitting { setFailure(failure.localizedDescription, for: worker) }
    }

    private func finished(_ name: String, process: Process) {
        guard workers[name] === process else { return }
        workers[name] = nil
        try? inputs.removeValue(forKey: name)?.close()
        if !quitting {
            if ProcessExit.succeeded(process) == true {
                setFailure(nil, for: name)
                if name == "projection" { CatalogUpdates.shared.notify() }
            } else {
                retryAfter[name] = Date().addingTimeInterval(30)
                setFailure("Maintenance stopped; retrying after a short delay", for: name)
            }
        }
    }

    private static func editorActive() -> Bool {
        let idle = CGEventSource.secondsSinceLastEventType(.combinedSessionState, eventType: CGEventType(rawValue: UInt32.max)!)
        return Indexing.activeEditing(bundleID: NSWorkspace.shared.frontmostApplication?.bundleIdentifier, idleSeconds: idle)
    }

    private func reportActivity() {
        guard Self.editorActive() else { return }
        for name in ["vectors", "preview-cache"] {
            // One-shot workers have no command reader. Interrupt cooperatively
            // at their next checkpoint when editing starts; later ticks retry.
            if let process = workers[name], process.isRunning { process.terminate() }
        }
    }

    func stop() {
        quitting = true
        activityTimer?.invalidate(); activityTimer = nil
        for input in inputs.values { try? input.close() }
        inputs.removeAll()
        for process in workers.values where process.isRunning { process.terminate() }
        let deadline = ContinuousClock.now.advanced(by: Self.shutdownGrace)
        while workers.values.contains(where: \.isRunning), ContinuousClock.now < deadline {
            Thread.sleep(forTimeInterval: 0.01)
        }
        for process in workers.values where process.isRunning {
            // A wedged writer must not retain its lock after the app quits.
            // These maintenance workers own no original-media deletion flow.
            _ = Darwin.kill(process.processIdentifier, SIGKILL)
        }
        workers.removeAll()
    }

    static func checkShutdown() throws {
        let owner = SearchMaintenance()
        let process = Process(), input = Pipe(), output = Pipe()
        process.executableURL = try Configuration.load().python
        process.arguments = ["-u", "-c", "import signal,sys,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready'); sys.stdin.read(); time.sleep(30)"]
        process.standardInput = input; process.standardOutput = output
        try process.run()
        defer { if process.isRunning { _ = Darwin.kill(process.processIdentifier, SIGKILL) } }
        var reader = LineReader(handle: output.fileHandleForReading)
        guard try reader.readLine(timeout: .seconds(2)) == Data("ready".utf8) else {
            throw AppError.message("Maintenance shutdown fixture did not start")
        }
        owner.workers["fixture"] = process
        owner.inputs["fixture"] = input.fileHandleForWriting
        owner.stop()
        process.waitUntilExit()
        guard process.terminationReason == .uncaughtSignal, process.terminationStatus == SIGKILL, owner.workers.isEmpty else {
            throw AppError.message("Unresponsive maintenance writer survived shutdown")
        }
        print("MAINTENANCE SHUTDOWN TEST PASSED: unresponsive owned writer reaped")
    }

    /// Reproduces the live crash: output EOF arrives while the worker still runs.
    static func checkExitAfterOutputCloses() async throws {
        let python = try Configuration.load().python
        let fixture = "import os,sys,time; print('{\"stage\":\"building\"}', flush=True); os.close(1); time.sleep(0.5); sys.exit(int(sys.argv[1]))"
        for (exitStatus, expectedFailure) in [(0, nil), (3, "Maintenance stopped; retrying after a short delay")] {
            let owner = SearchMaintenance()
            let process = Process(), input = Pipe(), output = Pipe()
            process.executableURL = python
            process.arguments = ["-c", fixture, String(exitStatus)]
            process.standardInput = input; process.standardOutput = output
            try process.run()
            defer { if process.isRunning { _ = Darwin.kill(process.processIdentifier, SIGKILL) } }
            owner.watch("fixture", process: process, input: input, output: output)
            let deadline = ContinuousClock.now.advanced(by: .seconds(10))
            while owner.workers["fixture"] != nil && ContinuousClock.now < deadline {
                try await Task.sleep(for: ProcessExit.pollInterval)
            }
            guard owner.workers.isEmpty, owner.inputs.isEmpty, !process.isRunning, process.terminationStatus == exitStatus else {
                throw AppError.message("Maintenance worker exiting with \(exitStatus) was not finished after its exit")
            }
            guard owner.status == "Updating visual search in the background", owner.failures["fixture"] == expectedFailure,
                  (owner.retryAfter["fixture"] != nil) == (expectedFailure != nil) else {
                throw AppError.message("Maintenance worker exiting with \(exitStatus) reported status '\(owner.status)', failure \(owner.failures["fixture"] ?? "none")")
            }
        }
        print("MAINTENANCE EXIT TEST PASSED: output EOF before exit waits for the exit status")
    }
}
