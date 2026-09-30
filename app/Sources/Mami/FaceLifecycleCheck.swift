import Foundation

/// Faces lane on an isolated, fully indexed fixture: waking for a recompute must
/// publish results, report the final counts, and release the worker when idle.
@MainActor func checkFaceLifecycle(at output: URL) async throws {
    guard ProcessInfo.processInfo.environment["MAMI_CATALOG"] != nil else {
        throw AppError.message("Face lifecycle checks require an isolated MAMI_CATALOG")
    }
    try FileManager.default.createDirectory(at: output, withIntermediateDirectories: false)
    let faces = Indexing.faces
    defer { faces.stop() }
    var phases: [String] = []
    let observer = Task { @MainActor in
        while !Task.isCancelled {
            if phases.last != faces.phase { phases.append(faces.phase); print("FACE LANE phase=\(faces.phase) running=\(faces.running) counts=\(String(describing: faces.queueCounts))"); fflush(stdout) }
            try? await Task.sleep(for: .milliseconds(50))
        }
    }
    defer { observer.cancel() }
    func wait(_ description: String, seconds: Int, _ predicate: () throws -> Bool) async throws {
        let deadline = ContinuousClock.now.advanced(by: .seconds(seconds))
        while !(try predicate()) {
            guard ContinuousClock.now < deadline else { throw AppError.message("Timed out: \(description); phases \(phases)") }
            try await Task.sleep(for: .milliseconds(100))
        }
        print("FACE LIFECYCLE \(description) passed"); fflush(stdout)
    }
    let generation = faces.catalogGeneration
    faces.recomputeFaces()
    try await wait("worker starts", seconds: 10) { faces.running }
    try await wait("recompute publishes", seconds: 120) { faces.catalogGeneration > generation }
    try await wait("final counts reported", seconds: 30) { faces.queueCounts?.remaining == 0 && faces.phase == "Faces ready" }
    try await wait("idle worker is released", seconds: 40) { !faces.running }
    let report: [String: Any] = ["status": "passed", "phases": phases]
    try JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted]).write(to: output.appendingPathComponent("result.json"), options: .withoutOverwriting)
    print("FACE LIFECYCLE PASSED")
}
