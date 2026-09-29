import Foundation

struct BackgroundWork: Sendable {
    let token: String
    let pending: Bool
    let paused: Bool
    let remaining: Int
    let completed: Int
    let failed: Int

    static func read(catalog: Catalog, preview: Bool, previousToken: String?) throws -> BackgroundWork? {
        let db = try SQLDatabase(catalog.database, readOnly: true)
        let token = try db.scalar("SELECT change_token FROM state WHERE id=1")
        guard token != previousToken else { return nil }
        let view = preview ? "mami_preview_work" : "mami_index_work"
        guard try db.scalar("SELECT count(*) FROM sqlite_master WHERE name=?", [view]) == "1" else { return nil }
        let jobs = preview ? "preview_jobs" : "index_jobs"
        let control = preview ? "preview_control" : "scan_control"
        try db.execute("BEGIN")
        let counts = try db.rows("SELECT state,count(*) FROM \(jobs) GROUP BY state")
        let complete = counts.first { $0[0] == "complete" }.flatMap { Int($0[1]) } ?? 0
        let failed = counts.first { $0[0] == "error" }.flatMap { Int($0[1]) } ?? 0
        let total = counts.reduce(0) { $0 + (Int($1[1]) ?? 0) }
        return BackgroundWork(token: token, pending: try db.scalar("SELECT 1 FROM \(view) LIMIT 1") == "1",
                              paused: try db.scalar("SELECT paused FROM \(control) WHERE id=1") == "1",
                              remaining: total-complete, completed: complete, failed: failed)
    }
}
