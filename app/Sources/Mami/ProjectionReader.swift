import Foundation
import SQLite3

/// Read-only paged access to generated search data; no original-media I/O.
struct ProjectionReader: Sendable {
    static let schemaVersion = "5"
    static let pageSize = 100
    let database: URL
    var sourceIdentity: String? = nil

    struct Scope: Sendable, Encodable {
        var camera: String?
        var shape: String?
        var kind: String?
        var from: String?
        var through: String?
        var assets: Set<String>?
        var arrivalThrough: Int64?
        var oldestFirst = false
    }
    struct Cursor: Sendable { let captured: String; let asset: String }
    struct Page: Sendable { let items: [Media]; let cursor: Cursor?; let total: Int }
    struct Facets: Sendable { let cameras: [String]; let earliest: String?; let total: Int; let sequence: Int64 }

    private func open() throws -> SQLDatabase {
        let db = try SQLDatabase(database, readOnly: true)
        do { return try validate(db) }
        catch {
            guard sqlite3_errcode(db.handle) == SQLITE_CANTOPEN else { throw error }
            // Apple SQLite needs initialized WAL/SHM files for a read-only WAL
            // connection, even when the main DB was fully checkpointed. Bootstrap
            // only those sidecars; never create a missing projection or edit rows.
            let owner = try SQLDatabase(database, createIfMissing: false)
            _ = try owner.scalar("PRAGMA user_version")
            try owner.preserveWALSidecars()
            let reader = try SQLDatabase(database, readOnly: true)
            return try withExtendedLifetime(owner) { try validate(reader) }
        }
    }

    private func validate(_ db: SQLDatabase) throws -> SQLDatabase {
        guard try db.scalar("PRAGMA user_version") == Self.schemaVersion,
              try db.scalar("SELECT ready FROM checkpoint WHERE id=1") == "1" else {
            throw AppError.message("Search catalog is still being prepared")
        }
        if let sourceIdentity, try db.scalar("SELECT identity FROM checkpoint WHERE id=1") != sourceIdentity {
            throw AppError.message("Search projection belongs to a different library")
        }
        return db
    }

    private func predicate(_ scope: Scope, db: SQLDatabase) throws -> (String, [String]) {
        var clauses: [String] = [], values: [String] = []
        for (column, value) in [("camera", scope.camera), ("shape", scope.shape), ("kind", scope.kind)] {
            if let value { clauses.append("\(column)=?"); values.append(value) }
        }
        if let from = scope.from { clauses.append("captured>=?"); values.append(from) }
        if let through = scope.through { clauses.append("captured<=? AND captured!=''"); values.append(through) }
        if let bound = scope.arrivalThrough { clauses.append("arrival<=?"); values.append(String(bound)) }
        if let assets = scope.assets {
            try db.execute("CREATE TEMP TABLE allowed_assets(asset TEXT PRIMARY KEY)")
            for asset in assets { try db.execute("INSERT INTO allowed_assets VALUES(?)", [asset]) }
            clauses.append("asset IN (SELECT asset FROM allowed_assets)")
        }
        return (clauses.isEmpty ? "1" : clauses.joined(separator: " AND "), values)
    }

    func page(scope: Scope, after: Cursor? = nil, limit: Int = Self.pageSize) throws -> Page {
        guard limit > 0 else { throw AppError.message("Page size must be positive") }
        let db = try open()
        try db.execute("BEGIN")
        let (base, values) = try predicate(scope, db: db)
        let total = Int(try db.scalar("SELECT count(*) FROM files WHERE \(base)", values)) ?? 0
        var whereClause = base, bindings = values
        if let after {
            if after.captured.isEmpty {
                whereClause += " AND captured='' AND asset>?"
                bindings.append(after.asset)
            } else {
                whereClause += " AND (captured='' OR captured\(scope.oldestFirst ? ">" : "<")? OR (captured=? AND asset>?))"
                bindings += [after.captured, after.captured, after.asset]
            }
        }
        // Empty capture dates already sort last in descending order. Adding an
        // expression there defeats files_date and sorts the entire catalog.
        let order = scope.oldestFirst ? "(captured=''),captured,asset" : "captured DESC,asset"
        let rows = try db.rows("SELECT asset,captured,summary FROM files WHERE \(whereClause) ORDER BY \(order) LIMIT ?", bindings + [String(limit)])
        let items = try rows.map { try JSONDecoder().decode(Media.self, from: Data($0[2].utf8)) }
        return Page(items: items, cursor: rows.last.map { Cursor(captured: $0[1], asset: $0[0]) }, total: total)
    }

    func media(paths: [String]) throws -> [Media] {
        guard !paths.isEmpty else { return [] }
        let db = try open()
        var items: [Media] = []
        for path in Set(paths) {
            if let row = try db.rows("SELECT summary FROM files WHERE path=? LIMIT 1", [path]).first {
                items.append(try JSONDecoder().decode(Media.self, from: Data(row[0].utf8)))
            }
        }
        return items
    }

    func frames(asset: String) throws -> [Sample] {
        try open().rows("SELECT frame,timestamp,crop FROM frames WHERE asset=? ORDER BY ordinal", [asset]).map {
            let crop = $0[2].isEmpty ? nil : try JSONDecoder().decode([Int]?.self, from: Data($0[2].utf8))
            return Sample(path: "", kind: "video", timestamp: Double($0[1]), frame: $0[0], score: nil, evidence: nil, crop: crop)
        }
    }

    func nearestFrame(asset: String, timestamp: Double?) throws -> Sample? {
        let db = try open()
        guard let file = try db.rows("SELECT path,kind FROM files WHERE asset=?", [asset]).first else { return nil }
        var candidates: [[String]] = []
        if let timestamp {
            candidates += try db.rows("SELECT frame,timestamp,crop FROM frames WHERE asset=? AND timestamp<=? ORDER BY timestamp DESC LIMIT 1", [asset,String(timestamp)])
            candidates += try db.rows("SELECT frame,timestamp,crop FROM frames WHERE asset=? AND timestamp>? ORDER BY timestamp LIMIT 1", [asset,String(timestamp)])
        } else {
            candidates = try db.rows("SELECT frame,timestamp,crop FROM frames WHERE asset=? ORDER BY ordinal LIMIT 1", [asset])
        }
        guard let row = candidates.min(by: { abs((Double($0[1]) ?? 0)-(timestamp ?? 0)) < abs((Double($1[1]) ?? 0)-(timestamp ?? 0)) }) else { return nil }
        let crop = row[2].isEmpty ? nil : try JSONDecoder().decode([Int]?.self, from: Data(row[2].utf8))
        return Sample(path: file[0], kind: file[1], timestamp: Double(row[1]), frame: row[0], score: nil, evidence: nil, crop: crop)
    }

    func facets() throws -> Facets {
        let db = try open()
        return Facets(cameras: try db.rows("SELECT DISTINCT camera FROM files ORDER BY camera").map { $0[0] },
                      earliest: try db.rows("SELECT min(captured) FROM files WHERE captured!=''").first?.first,
                      total: Int(try db.scalar("SELECT count(*) FROM files")) ?? 0,
                      sequence: Int64(try db.scalar("SELECT coalesce(max(sequence),0) FROM vector_events")) ?? 0)
    }

    func arrivals(after sequence: Int64) throws -> Int {
        Int(try open().scalar("SELECT count(*) FROM files WHERE arrival>?", [String(sequence)])) ?? 0
    }
}
