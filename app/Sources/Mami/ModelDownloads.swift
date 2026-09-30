import Foundation
import MamiCore
import Darwin

@MainActor final class ModelDownloads: ObservableObject {
    static let shared = ModelDownloads()
    @Published private(set) var running = false
    @Published private(set) var status = "Indexing models are stored locally for offline use."
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
                    while task.isRunning {
                        do {
                            let line = try reader.readLine(timeout: .seconds(65))
                            let value = try JSONDecoder().decode(Progress.self, from: line)
                            await self?.update(value.status)
                        } catch WorkerTransportError.timedOut { continue }
                    }
                } catch WorkerTransportError.closed { }
                catch { await self?.update("Model download failed: \(error.localizedDescription)") }
                await self?.finished(task)
            }
        } catch { status = "Could not start model download: \(error.localizedDescription)" }
    }

    private struct Progress: Decodable { let status: String }
    private func update(_ message: String) { status = message }
    private func finished(_ task: Process) async {
        guard process === task else { return }
        if task.isRunning {
            task.terminate()
            let deadline = ContinuousClock.now.advanced(by: .seconds(2))
            while task.isRunning && ContinuousClock.now < deadline { try? await Task.sleep(for: .milliseconds(25)) }
            if task.isRunning {
                _ = Darwin.kill(task.processIdentifier, SIGKILL)
                running = false; process = nil; status = "Model download stopped after a protocol failure. Try again."
                return
            }
        }
        running = false; process = nil
        if task.terminationStatus == 0 {
            status = "Indexing models installed."
            Indexing.shared.retry()
            Indexing.faces.retry()
        } else { status = "Model download did not complete. Check the connection and try again." }
    }
    func stop() { process?.terminate() }
}
