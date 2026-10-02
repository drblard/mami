import Foundation
import Observation
import MamiCore
import Darwin

@MainActor @Observable final class ModelDownloads {
    static let shared = ModelDownloads()
    private(set) var running = false
    private(set) var status = "Indexing models are stored locally for offline use."
    private var process: Process?

    func download() {
        guard !running else { return }
        do {
            let configuration = try Configuration.load()
            let task = Process(), output = Pipe()
            task.executableURL = configuration.python
            task.environment = configuration.workerEnvironment
            task.arguments = ["-B", configuration.worker.deletingLastPathComponent().appendingPathComponent("download_models.py").path]
            task.qualityOfService = .utility
            task.standardOutput = output
            task.standardError = AppDiagnostics.workerErrorOutput
            try task.run()
            process = task; running = true; status = "Downloading pinned indexing models…"
            Task.detached { [weak self] in
                var reader = LineReader(handle: output.fileHandleForReading)
                do {
                    while true {
                        do {
                            let line = try reader.readLine(timeout: .seconds(65))
                            let value = try JSONDecoder().decode(Progress.self, from: line)
                            await self?.update(value.status)
                        } catch WorkerTransportError.timedOut where !task.isRunning { break }
                        catch WorkerTransportError.timedOut { continue }
                    }
                } catch WorkerTransportError.closed { }
                catch { await self?.update("Model download failed: \(error.localizedDescription)") }
                await self?.finished(task)
            }
        } catch { status = "Could not start model download: \(error.localizedDescription)" }
    }

    private static let exitGrace: Duration = .seconds(2)
    private struct Progress: Decodable { let status: String }
    private func update(_ message: String) { status = message }
    private func finished(_ task: Process) async {
        guard process === task else { return }
        // Output EOF usually precedes the observed exit of a successful download.
        if !(await ProcessExit.wait(for: task, grace: Self.exitGrace)) {
            task.terminate()
            if !(await ProcessExit.wait(for: task, grace: Self.exitGrace)) {
                _ = Darwin.kill(task.processIdentifier, SIGKILL)
                running = false; process = nil; status = "Model download stopped after a protocol failure. Try again."
                return
            }
        }
        running = false; process = nil
        if ProcessExit.succeeded(task) == true {
            status = "Indexing models installed."
            Indexing.shared.retry()
            Indexing.faces.retry()
        } else { status = "Model download did not complete. Check the connection and try again." }
    }
    func stop() { process?.terminate() }
}
