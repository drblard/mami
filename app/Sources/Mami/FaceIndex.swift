import Foundation

struct FaceItem: Identifiable, Hashable, Sendable {
    enum Source: String, Sendable { case confirmed, suggested, track }
    let id: Int64
    let ref: FaceRef
    let crop: String
    let source: Source?
    let similarity: Double?
}

struct FaceGroup: Identifiable, Hashable, Sendable {
    let id: Int64
    let count: Int
    let media: Int
    let cover: String
}

struct PersonCounts: Hashable, Sendable {
    var media = 0
    var confirmedFaces = 0
    /// Suggestions below `FaceIndex.automaticSimilarity`: shown for review.
    var toCheckFaces = 0
    /// Suggestions at or above it: accepted without review unless rejected.
    var automaticFaces = 0
}

/// The three kinds of faces on a person's page.
enum PersonFaces: String, CaseIterable, Sendable {
    case toCheck, automatic, confirmed
}

/// Read-only queries over the generated face index written by face_worker.py.
enum FaceIndex {
    static let modelName = "antelopev2"
    static let modelFiles = ["scrfd_10g_bnkps.onnx", "glintr100.onnx"]
    /// A person seen in a single photo or clip is usually a passer-by; such groups
    /// stay out of review (naming someone still finds them through suggestions).
    static let minimumGroupMedia = 2
    /// Must equal faces_store.FACES_SCHEMA_VERSION; older indexes are migrated by the worker.
    static let schemaVersion = 3
    /// Suggestions at least this similar to a confirmed face are accepted without
    /// review (a rejection still removes them). Chosen from a banded review of the
    /// live library: every sampled face at >= 0.65 was the right person, while
    /// doubtful matches began below 0.65 and clear mistakes below 0.55.
    static let automaticSimilarity = 0.65
    static let groupLimit = 300
    static let faceLimit = 2000
    /// Must equal faces_store.MAX_FACE_JOB_ATTEMPTS.
    static let maximumJobAttempts = 3

    static var modelDirectory: URL {
        let environment = ProcessInfo.processInfo.environment
        let models = environment["MAMI_MODELS"].map { URL(fileURLWithPath: $0) } ?? AppPaths(environment: environment).models
        return models.appendingPathComponent("faces/\(modelName)")
    }
    static var modelInstalled: Bool {
        modelFiles.allSatisfy { FileManager.default.fileExists(atPath: modelDirectory.appendingPathComponent($0).path) }
    }

    // SQLDatabase binds text; comparisons against computed values need an explicit CAST.
    private static func open(_ catalog: Catalog) throws -> SQLDatabase? {
        let url = catalog.facesDatabase
        guard FileManager.default.fileExists(atPath: url.path) else { return nil }
        // WAL readers need the -shm file, which SQLite removes when the worker
        // closes cleanly; a read-only handle cannot recreate it. Open without
        // create permission and only ever query.
        let db = try SQLDatabase(url, readOnly: false, createIfMissing: false)
        guard try db.hasTable("face_assignments"),
              Int(try db.scalar("SELECT version FROM faces_schema")) ?? 0 >= schemaVersion else { return nil }
        return db
    }

    /// Worker readiness: pending face jobs, or catalog assets not yet mirrored,
    /// but never while previews or AI search still have work (PIPELINE.md order).
    static func backgroundWork(catalog: Catalog, previousToken: String?) throws -> BackgroundWork? {
        let source = try SQLDatabase(catalog.database, readOnly: true)
        guard try source.hasTable("preview_jobs") else { return nil }
        let faces = try open(catalog)
        // An index from an older build needs the worker to migrate and regroup it.
        let outdated = FileManager.default.fileExists(atPath: catalog.facesDatabase.path) && faces == nil
        let facesToken = try faces?.scalar("SELECT change_token FROM face_state WHERE id=1") ?? "none"
        let token = try source.scalar("SELECT change_token FROM state WHERE id=1") + ":" + facesToken
        guard token != previousToken else { return nil }
        let upstream = try ["mami_preview_work", "mami_index_work"].contains { view in
            try source.scalar("SELECT count(*) FROM sqlite_master WHERE name=?", [view]) == "1"
                && source.scalar("SELECT 1 FROM \(view) LIMIT 1") == "1"
        }
        let eligible = Int(try source.scalar("SELECT count(*) FROM index_jobs j JOIN preview_jobs p ON p.asset=j.asset WHERE p.state='complete'")) ?? 0
        var complete = 0, failed = 0, total = 0, queued = false, paused = false
        if let faces {
            for row in try faces.rows("SELECT state,count(*) FROM face_jobs GROUP BY state") {
                let count = Int(row[1]) ?? 0
                total += count
                if row[0] == "complete" { complete = count } else if row[0] == "error" { failed = count } else { queued = queued || count > 0 }
            }
            let retryable = try faces.scalar("SELECT 1 FROM face_jobs WHERE state='error' AND attempts<? LIMIT 1", [String(maximumJobAttempts)]) == "1"
            queued = queued || retryable
            paused = try faces.scalar("SELECT paused FROM face_state WHERE id=1") == "1"
        }
        let unsynced = eligible != total
        return BackgroundWork(token: token, pending: outdated || (!upstream && (queued || unsynced)), paused: paused,
                              remaining: max(eligible - complete, total - complete), completed: complete, failed: failed)
    }

    private static func face(_ row: [String], source: Int? = nil) -> FaceItem? {
        guard let id = Int64(row[0]) else { return nil }
        let box = row[3...6].compactMap(Double.init)
        guard box.count == 4 else { return nil }
        let ref = FaceRef(asset: row[1], timestamp: row[2].isEmpty ? nil : Double(row[2]), box: box)
        return FaceItem(id: id, ref: ref, crop: row[7], source: source.flatMap { FaceItem.Source(rawValue: row[$0]) },
                        similarity: source.flatMap { Double(row[$0 + 1]) })
    }
    private static let faceColumns = "f.id,f.asset,f.timestamp,f.x1,f.y1,f.x2,f.y2,f.crop"

    /// Unnamed groups, most-seen first. By default only groups mostly from the
    /// user's own camera; screenshots and saved media are full of strangers.
    static func groups(includeSavedMedia: Bool = false, _ catalog: Catalog = .standard) throws -> [FaceGroup] {
        guard let db = try open(catalog) else { return [] }
        return try db.rows("""
            SELECT g.grp, count(*) AS size, count(DISTINCT f.asset) AS media, sum(j.origin='camera') AS own,
                   (SELECT crop FROM faces WHERE id=g.grp)
            FROM face_groups g JOIN faces f ON f.id=g.face JOIN face_jobs j ON j.asset=f.asset
            GROUP BY g.grp HAVING media >= CAST(? AS INTEGER) AND (CAST(? AS INTEGER)=1 OR own*2 >= size)
            ORDER BY media DESC, size DESC, g.grp LIMIT ?
            """, [String(minimumGroupMedia), includeSavedMedia ? "1" : "0", String(groupLimit)]).compactMap { row in
            guard let id = Int64(row[0]), let count = Int(row[1]), let media = Int(row[2]) else { return nil }
            return FaceGroup(id: id, count: count, media: media, cover: row[4])
        }
    }

    static func faces(group: Int64, _ catalog: Catalog = .standard) throws -> [FaceItem] {
        guard let db = try open(catalog) else { return [] }
        // Least typical first: outliers (possibly someone else) are seen before the obvious matches.
        return try db.rows("SELECT \(faceColumns) FROM face_groups g JOIN faces f ON f.id=g.face WHERE g.grp=? AND f.crop IS NOT NULL ORDER BY g.typical, f.id LIMIT ?",
                           [String(group), String(faceLimit)]).compactMap { face($0) }
    }

    static func faces(person: String, kind: PersonFaces, _ catalog: Catalog = .standard) throws -> [FaceItem] {
        guard let db = try open(catalog) else { return [] }
        let filter: String
        switch kind {
        case .confirmed: filter = "a.source='confirmed'"
        case .toCheck: filter = "a.source='suggested' AND a.similarity < CAST(?2 AS REAL)"
        case .automatic: filter = "a.source='suggested' AND a.similarity >= CAST(?2 AS REAL)"
        }
        // To check: least certain first. Automatic: also least certain first, so a
        // glance at the top shows the matches most worth a second look.
        return try db.rows("""
            SELECT \(faceColumns), a.source, a.similarity FROM face_assignments a JOIN faces f ON f.id=a.face
            WHERE a.person=?1 AND f.crop IS NOT NULL AND \(filter)
            ORDER BY a.similarity, f.id LIMIT ?3
            """, [person, String(automaticSimilarity), String(faceLimit)]).compactMap { face($0, source: 8) }
    }

    static func counts(_ catalog: Catalog = .standard) throws -> [String: PersonCounts] {
        guard let db = try open(catalog) else { return [:] }
        var result: [String: PersonCounts] = [:]
        for row in try db.rows("SELECT a.person, count(DISTINCT f.asset) FROM face_assignments a JOIN faces f ON f.id=a.face GROUP BY a.person") {
            result[row[0], default: PersonCounts()].media = Int(row[1]) ?? 0
        }
        for row in try db.rows("""
            SELECT person, sum(source='confirmed'), sum(source='suggested' AND similarity < CAST(? AS REAL)),
                   sum(source='suggested' AND similarity >= CAST(? AS REAL))
            FROM face_assignments WHERE source IN ('confirmed','suggested') GROUP BY person
            """, [String(automaticSimilarity), String(automaticSimilarity)]) {
            result[row[0], default: PersonCounts()].confirmedFaces = Int(row[1]) ?? 0
            result[row[0], default: PersonCounts()].toCheckFaces = Int(row[2]) ?? 0
            result[row[0], default: PersonCounts()].automaticFaces = Int(row[3]) ?? 0
        }
        return result
    }

    /// Assets showing every requested person (confirmed, suggested or tracked).
    static func assets(withAll people: Set<String>, _ catalog: Catalog = .standard) throws -> Set<String> {
        guard !people.isEmpty, let db = try open(catalog) else { return [] }
        var result: Set<String>?
        for person in people.sorted() {
            let assets = Set(try db.rows("SELECT DISTINCT f.asset FROM face_assignments a JOIN faces f ON f.id=a.face WHERE a.person=?", [person]).map { $0[0] })
            result = result.map { $0.intersection(assets) } ?? assets
        }
        return result ?? []
    }
}
