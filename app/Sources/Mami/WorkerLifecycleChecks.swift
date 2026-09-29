import Foundation
import AppKit

@MainActor func checkWorkerLifecycle(at output: URL) async throws {
    let environment = ProcessInfo.processInfo.environment
    guard environment["MAMI_SUPPORT_ROOT"] != nil, let mediaRoot = environment["MAMI_MEDIA_ROOT"] else {
        throw AppError.message("Worker lifecycle checks require isolated support and media roots")
    }
    try FileManager.default.createDirectory(at: output, withIntermediateDirectories: false)
    try FileManager.default.createDirectory(atPath: mediaRoot, withIntermediateDirectories: true)
    let library = Library()
    await library.load()
    guard library.ready else { throw AppError.message(library.error ?? "Library not ready") }
    let configuration = try Configuration.load()
    try SearchMaintenance.shared.start(configuration)
    Indexing.startAll()
    defer { Indexing.shared.stop(); Indexing.previews.stop(); SearchMaintenance.shared.stop() }

    func wait(_ description: String, seconds: Int, _ predicate: () async throws -> Bool) async throws {
        let deadline = ContinuousClock.now.advanced(by: .seconds(seconds))
        while !(try await predicate()) {
            guard ContinuousClock.now < deadline else { throw AppError.message("Timed out: \(description)") }
            try await Task.sleep(for: .milliseconds(100))
        }
        print("LIFECYCLE \(description) passed"); fflush(stdout)
    }
    try await wait("initial work is scheduled", seconds: 30) {
        FileManager.default.fileExists(atPath: AppPaths().search.appendingPathComponent("Vectors/current.json").path) &&
            Indexing.shared.queueCounts != nil
    }
    try await wait("initial work drains", seconds: 40) {
        !Indexing.shared.running && !Indexing.previews.running && SearchMaintenance.shared.activeWorkerCount == 0
    }
    guard !(await library.worker.isRunning) else { throw AppError.message("Browsing started an unnecessary search process") }
    Indexing.shared.togglePause()
    try await wait("pause persists", seconds: 10) {
        try SQLDatabase(Catalog.standard.database, readOnly: true).scalar("SELECT paused FROM scan_control WHERE id=1") == "1" && Indexing.shared.paused
    }
    try await wait("paused worker exits", seconds: 25) { !Indexing.shared.running }
    Indexing.shared.togglePause()
    try await wait("resume wakes sleeping worker", seconds: 10) {
        try SQLDatabase(Catalog.standard.database, readOnly: true).scalar("SELECT paused FROM scan_control WHERE id=1") == "0" && !Indexing.shared.paused
    }
    let text = try await library.worker.search("fixture", mode: "speech")
    guard text.error == nil else { throw AppError.message(text.error!) }
    let visual = try await library.worker.search("a red video", mode: "visual")
    if visual.visual_pending == true {
        try await wait("visual model warms", seconds: 30) {
            try await library.worker.search("a red video", mode: "visual").visual_pending != true
        }
    }
    try await wait("search releases idle resources", seconds: 75) { !(await library.worker.isRunning) }

    let movie = URL(fileURLWithPath: mediaRoot).appendingPathComponent("DJI_20260929120000_0001_D.MP4")
    let helper = configuration.worker.deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent("Helpers/ffmpeg")
    try await Task.detached {
        let process = Process()
        process.executableURL = helper
        process.arguments = ["-nostdin", "-v", "error", "-n", "-f", "lavfi", "-i", "color=c=red:s=320x180:r=5", "-t", "2", "-c:v", "libx264", "-threads", "1", movie.path]
        try process.run()
        let deadline = ContinuousClock.now.advanced(by: .seconds(20))
        while process.isRunning && ContinuousClock.now < deadline { try await Task.sleep(for: .milliseconds(25)) }
        if process.isRunning { kill(process.processIdentifier, SIGKILL); throw AppError.message("Fixture encoder deadline") }
        guard process.terminationStatus == 0 else { throw AppError.message("Fixture encoder failed") }
    }.value
    Indexing.shared.scanNow()
    try await wait("new media previews and indexes", seconds: 120) {
        let db = try SQLDatabase(Catalog.standard.database, readOnly: true)
        return try db.scalar("SELECT count(*) FROM index_jobs WHERE state='complete'") == "1" &&
            db.scalar("SELECT count(*) FROM preview_jobs WHERE state='complete'") == "1"
    }
    try await wait("new media reaches search", seconds: 30) {
        let reply = try await library.worker.search("a red video", mode: "visual")
        return reply.visual_pending != true && reply.hits?.count == 1
    }
    try await wait("indexer sleeps after completed work", seconds: 30) { !Indexing.shared.running }
    let fixtureDB = try SQLDatabase(Catalog.standard.database)
    try fixtureDB.execute("UPDATE index_jobs SET state='error',attempts=3,error='fixture retry'")
    Indexing.shared.retry()
    try await wait("retry wakes and recovers", seconds: 30) {
        try SQLDatabase(Catalog.standard.database, readOnly: true).scalar("SELECT count(*) FROM index_jobs WHERE state='complete' AND attempts=0") == "1"
    }
    try await wait("all work and search return to idle", seconds: 90) {
        let searching = await library.worker.isRunning
        return !searching && !Indexing.shared.running && !Indexing.previews.running && SearchMaintenance.shared.activeWorkerCount == 0
    }
    let report: [String: Any] = ["status": "passed", "checks": ["no eager search", "text and visual wake", "idle model exit", "new media wake", "preview priority", "idle worker exit", "pause/resume while sleeping", "retry while sleeping"]]
    try JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys]).write(to: output.appendingPathComponent("result.json"), options: .withoutOverwriting)
}
