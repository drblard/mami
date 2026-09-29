import Foundation

struct MaintenanceWork: Sendable {
    let projection: Bool
    let vectors: Bool
    let previews: Bool

    static func read(catalog: Catalog, projection: URL, vectors: URL) throws -> MaintenanceWork {
        let source = try SQLDatabase(catalog.database, readOnly: true)
        guard try source.hasTable("search_events"), FileManager.default.fileExists(atPath: projection.path) else {
            return MaintenanceWork(projection: true, vectors: false, previews: false)
        }
        let db = try SQLDatabase(projection, readOnly: true)
        let sequence = try source.scalar("SELECT coalesce(max(sequence),0) FROM search_events")
        let checkpoint = try db.rows("SELECT sequence,ready FROM checkpoint WHERE id=1").first
        let projectionPending = checkpoint == nil || checkpoint?[1] != "1" || checkpoint?[0] != sequence
        let vectorSequence = Int64(try db.scalar("SELECT coalesce(max(sequence),0) FROM vector_events")) ?? 0
        let pointer = try? JSONDecoder().decode([String: String].self, from: Data(contentsOf: vectors.appendingPathComponent("current.json")))
        let generation = pointer?["generation"].map { vectors.appendingPathComponent($0) } ?? vectors
        let manifest = (try? JSONSerialization.jsonObject(with: Data(contentsOf: generation.appendingPathComponent("manifest.json")))) as? [String: Any]
        let baseSequence = (manifest?["projection_sequence"] as? NSNumber)?.int64Value
        var previewPending = false
        for table in ["preview_pack_pending", "preview_retire_pending"] where try source.hasTable(table) {
            if try source.scalar("SELECT 1 FROM \(table) LIMIT 1") == "1" { previewPending = true }
        }
        if try source.hasTable("preview_generations") {
            if !previewPending {
                previewPending = try source.scalar("SELECT 1 FROM preview_generations g WHERE obsolete=1 AND review IS NULL AND EXISTS (SELECT 1 FROM preview_generations n WHERE n.asset=g.asset AND n.id>g.id AND n.obsolete=1) LIMIT 1") == "1"
            }
        }
        return MaintenanceWork(projection: projectionPending, vectors: baseSequence.map { vectorSequence > $0 } ?? true, previews: previewPending)
    }
}
