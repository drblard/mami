import Foundation
import SQLite3
import Darwin
import MamiCore

/// All catalog access (including backups) shares a process and filesystem lock.
/// No network or source-media writes are involved.
struct Catalog: Sendable {
    static let standard = configured()

    static func configured(environment: [String: String] = ProcessInfo.processInfo.environment,
                           home: URL = FileManager.default.homeDirectoryForCurrentUser) -> Catalog {
        if let override = environment["MAMI_CATALOG"] {
            return Catalog(directory: URL(fileURLWithPath: override))
        }
        return Catalog(directory: home.appendingPathComponent("mami-lab/catalog/database"),
                       backups: home.appendingPathComponent("mami-lab/backups/catalog"),
                       legacyAnnotations: home.appendingPathComponent("mami-lab/catalog/annotations"),
                       artifacts: home.appendingPathComponent("mami-lab/index-artifacts"))
    }
    private static let lock = NSRecursiveLock()
    let directory: URL
    let backups: URL
    let legacyAnnotationsDirectory: URL
    let artifacts: URL
    var database: URL { directory.appendingPathComponent("catalog.sqlite") }
    var userDatabase: URL { directory.appendingPathComponent("user.sqlite") }
    var userBackups: URL { backups.appendingPathComponent("user-state") }
    private static let userTables = PersonalDataMigration.tables

    init(directory: URL, backups: URL? = nil, legacyAnnotations: URL? = nil, artifacts: URL? = nil) {
        self.directory = directory
        self.backups = backups ?? directory.appendingPathComponent("backups")
        self.legacyAnnotationsDirectory = legacyAnnotations ?? directory.appendingPathComponent("legacy-annotations")
        self.artifacts = artifacts ?? directory.appendingPathComponent("index-artifacts")
    }

    private func access<T>(_ body: (SQLDatabase) throws -> T) throws -> T {
        Self.lock.lock()
        defer { Self.lock.unlock() }
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let fd = Darwin.open(directory.appendingPathComponent("catalog.lock").path, O_CREAT | O_RDWR, S_IRUSR | S_IWUSR)
        guard fd >= 0 else { throw AppError.message("Cannot open catalog lock") }
        defer { Darwin.close(fd) }
        guard flock(fd, LOCK_EX) == 0 else { throw AppError.message("Cannot lock catalog") }
        defer { flock(fd, LOCK_UN) }
        let db = try SQLDatabase(database)
        try db.execute("PRAGMA journal_mode=PERSIST")
        try db.execute("PRAGMA synchronous=FULL")
        let version = try db.scalar("PRAGMA user_version")
        guard ["0", "1", "2"].contains(version) else { throw AppError.message("Catalog was created by a newer Mami version") }
        if version == "0" {
            try db.transaction {
                try db.execute("CREATE TABLE state (id INTEGER PRIMARY KEY CHECK(id=1), identity TEXT NOT NULL, revision INTEGER NOT NULL)")
                let identity: String
                if FileManager.default.fileExists(atPath: userDatabase.path) {
                    let user = try SQLDatabase(userDatabase, readOnly: true)
                    guard try user.scalar("SELECT version FROM user_store_info") == "1" else { throw AppError.message("Unsupported personal-data database") }
                    identity = try user.scalar("SELECT source_identity FROM user_store_info")
                } else { identity = UUID().uuidString }
                try db.execute("INSERT INTO state VALUES(1, ?, 0)", [identity])
                try db.execute("CREATE TABLE media (path TEXT PRIMARY KEY, asset TEXT NOT NULL, payload TEXT NOT NULL)")
                try db.execute("CREATE INDEX media_asset ON media(asset)")
                try db.execute("CREATE TABLE annotations (asset TEXT PRIMARY KEY, payload TEXT NOT NULL)")
                try db.execute("CREATE TABLE annotation_history (id INTEGER PRIMARY KEY, asset TEXT NOT NULL, payload TEXT NOT NULL, updated TEXT NOT NULL)")
                try db.execute("CREATE TABLE imported_events (name TEXT PRIMARY KEY)")
                for table in ["media", "annotations", "annotation_history", "imported_events"] {
                    for operation in ["INSERT", "UPDATE", "DELETE"] {
                        try db.execute("CREATE TRIGGER \(table)_\(operation) AFTER \(operation) ON \(table) BEGIN UPDATE state SET revision=revision+1 WHERE id=1; END")
                    }
                }
                try db.execute("PRAGMA user_version=1")
            }
        }
        if version == "0" || version == "1" {
            try db.transaction {
                try db.execute("ALTER TABLE state ADD COLUMN change_token TEXT NOT NULL DEFAULT ''")
                try db.execute("UPDATE state SET change_token=lower(hex(randomblob(16)))")
                for table in ["media", "annotations", "annotation_history", "imported_events"] {
                    for operation in ["INSERT", "UPDATE", "DELETE"] {
                        try db.execute("DROP TRIGGER \(table)_\(operation)")
                        try db.execute("CREATE TRIGGER \(table)_\(operation) AFTER \(operation) ON \(table) BEGIN UPDATE state SET revision=revision+1,change_token=lower(hex(randomblob(16))) WHERE id=1; END")
                    }
                }
                try db.execute("PRAGMA user_version=2")
            }
        }
        return try body(db)
    }

    private static func json<T: Encodable>(_ value: T) throws -> String {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        return String(decoding: try encoder.encode(value), as: UTF8.self)
    }

    /// The existing catalog lock serializes migration and both stores' writers.
    /// Publish the new user database only after copying, pruning and verification.
    private func userAccess<T>(_ body: (SQLDatabase) throws -> T) throws -> T {
        try access { source in
            let identity = try source.scalar("SELECT identity FROM state WHERE id=1")
            if !FileManager.default.fileExists(atPath: userDatabase.path) {
                guard try source.rows("SELECT name FROM sqlite_master WHERE name='user_store_migration'").isEmpty else {
                    throw AppError.message("Personal-data database is missing. Restore a user-data backup; it cannot be regenerated.")
                }
                try PersonalDataMigration.create(from: database, at: userDatabase, identity: identity)
            }
            let user = try SQLDatabase(userDatabase)
            guard try user.scalar("SELECT version FROM user_store_info WHERE source_identity=?", [identity]) == String(PersonalDataMigration.version),
                  try user.scalar("SELECT identity FROM state WHERE id=1") == identity else {
                throw AppError.message("User data belongs to another catalog or a newer version. Restore into a new directory.")
            }
            if try !source.hasTable("user_store_migration") {
                try source.transaction {
                    try source.execute("CREATE TABLE user_store_migration(id INTEGER PRIMARY KEY CHECK(id=1), source_identity TEXT NOT NULL, mirror_enabled INTEGER NOT NULL DEFAULT 0)")
                    try source.execute("INSERT INTO user_store_migration(id,source_identity) VALUES(1,?)", [identity])
                    try protectLegacyUserTables(in: source)
                }
            }
            try user.execute("PRAGMA journal_mode=PERSIST")
            try user.execute("PRAGMA synchronous=FULL")
            return try body(user)
        }
    }

    func prepareUserStore() throws { try userAccess { _ in } }

    private func protectLegacyUserTables(in source: SQLDatabase) throws {
        for table in Self.userTables where try source.hasTable(table) {
            for operation in ["INSERT", "UPDATE", "DELETE"] {
                try source.execute("CREATE TRIGGER IF NOT EXISTS user_owned_\(table)_\(operation) BEFORE \(operation) ON \(table) WHEN (SELECT mirror_enabled FROM user_store_migration WHERE id=1)=0 BEGIN SELECT RAISE(ABORT,'Personal data moved to user.sqlite; open the current Mami build.'); END")
            }
        }
    }

    /// Explicit full exports also contain current personal data. This is used
    /// by restore/diagnostic checks, never by the automatic backup scheduler.
    private func mirrorUserState(into db: SQLDatabase) throws {
        guard FileManager.default.fileExists(atPath: userDatabase.path) else { return }
        try db.execute("ATTACH DATABASE ? AS personal", [userDatabase.path])
        defer { try? db.execute("DETACH DATABASE personal") }
        let token = try db.scalar("SELECT change_token FROM personal.state WHERE id=1")
        try db.execute("CREATE TABLE IF NOT EXISTS user_mirror(token TEXT NOT NULL)")
        if try db.scalar("SELECT token FROM user_mirror") == token { return }
        try db.transaction {
            try db.execute("UPDATE user_store_migration SET mirror_enabled=1 WHERE id=1")
            for table in Self.userTables {
                guard let schema = try db.rows("SELECT sql FROM personal.sqlite_master WHERE type='table' AND name=?", [table]).first?.first else { continue }
                let exists = !(try db.rows("SELECT name FROM main.sqlite_master WHERE type='table' AND name=?", [table])).isEmpty
                if !exists { try db.execute(schema) }
                if try db.rows("SELECT * FROM main.\(table) ORDER BY 1") != db.rows("SELECT * FROM personal.\(table) ORDER BY 1") {
                    try db.execute("DELETE FROM main.\(table)")
                    try db.execute("INSERT INTO main.\(table) SELECT * FROM personal.\(table)")
                    try db.execute("UPDATE state SET revision=revision+1,change_token=lower(hex(randomblob(16))) WHERE id=1")
                }
                for row in try db.rows("SELECT name,sql FROM personal.sqlite_master WHERE type='trigger' AND tbl_name=?", [table]) {
                    if try db.rows("SELECT name FROM main.sqlite_master WHERE type='trigger' AND name=?", [row[0]]).isEmpty { try db.execute(row[1]) }
                }
            }
            try db.execute("DELETE FROM user_mirror")
            try db.execute("INSERT INTO user_mirror VALUES(?)", [token])
            try protectLegacyUserTables(in: db)
            try db.execute("UPDATE user_store_migration SET mirror_enabled=0 WHERE id=1")
        }
    }

    func synchronize(_ items: [Media], source: String? = nil) throws {
        try access { db in
            try db.transaction {
                if let source {
                    try db.execute("CREATE TABLE IF NOT EXISTS seed_sources(source TEXT PRIMARY KEY)")
                    if !(try db.rows("SELECT source FROM seed_sources WHERE source=?", [source])).isEmpty { return }
                }
                for item in items {
                    // Preserve the logical index key and content identity when
                    // a verified relocation changes its physical file path.
                    for row in try db.rows("SELECT path,payload FROM media WHERE asset=? AND path != ?", [item.assetID, item.url.path]) {
                        let old = try JSONDecoder().decode(Media.self, from: Data(row[1].utf8))
                        if old.path == item.path {
                            try db.execute("UPDATE media SET path=?,payload=? WHERE path=?", [item.url.path, try Self.json(item), row[0]])
                        }
                    }
                    try db.execute("INSERT INTO media VALUES(?,?,?) ON CONFLICT(path) DO UPDATE SET asset=excluded.asset,payload=excluded.payload WHERE media.asset != excluded.asset OR media.payload != excluded.payload",
                                   [item.url.path, item.assetID, try Self.json(item)])
                }
                if let source {
                    try db.execute("INSERT INTO seed_sources VALUES(?)", [source])
                    try db.execute("UPDATE state SET revision=revision+1,change_token=lower(hex(randomblob(16))) WHERE id=1")
                }
            }
        }
    }

    func media() throws -> [Media] {
        try access { db in
            try db.rows("SELECT payload FROM media ORDER BY path").map {
                try JSONDecoder().decode(Media.self, from: Data($0[0].utf8))
            }
        }
    }

    func identity() throws -> String {
        try access { try $0.onlyRow("SELECT identity FROM state WHERE id=1")[0] }
    }

    func media(forAssetIDs assetIDs: [String]) throws -> [Media] {
        guard !assetIDs.isEmpty else { return [] }
        return try access { db in
            let uniqueIDs = Array(Set(assetIDs)).sorted()
            let batchSize = Int(sqlite3_limit(db.handle, SQLITE_LIMIT_VARIABLE_NUMBER, -1))
            var result: [Media] = []
            for start in stride(from: 0, to: uniqueIDs.count, by: batchSize) {
                let batch = Array(uniqueIDs[start..<min(start + batchSize, uniqueIDs.count)])
                let placeholders = Array(repeating: "?", count: batch.count).joined(separator: ",")
                let rows = try db.rows("SELECT payload FROM media WHERE asset IN (\(placeholders)) ORDER BY path", batch)
                result += try rows.map { try JSONDecoder().decode(Media.self, from: Data($0[0].utf8)) }
            }
            return result
        }
    }

    private func photosTables(_ db: SQLDatabase) throws {
        try db.execute("CREATE TABLE IF NOT EXISTS photos_import_history(resource TEXT PRIMARY KEY, digest TEXT NOT NULL, size TEXT NOT NULL)")
        try db.execute("CREATE INDEX IF NOT EXISTS photos_import_digest ON photos_import_history(digest)")
        try db.execute("CREATE TABLE IF NOT EXISTS media_roots(path TEXT PRIMARY KEY)")
        for table in ["photos_import_history", "media_roots"] {
            try db.execute("CREATE TRIGGER IF NOT EXISTS \(table)_INSERT AFTER INSERT ON \(table) BEGIN UPDATE state SET revision=revision+1,change_token=lower(hex(randomblob(16))) WHERE id=1; END")
        }
    }
    func photosHistory() throws -> Set<String> {
        try userAccess { db in
            try photosTables(db)
            return Set(try db.rows("SELECT resource FROM photos_import_history").map { $0[0] })
        }
    }
    func recordPhotosImport(resource: String, digest: String, size: Int64) throws {
        try userAccess { db in
            try photosTables(db)
            try db.execute("INSERT OR IGNORE INTO photos_import_history VALUES(?,?,?)", [resource, digest, String(size)])
        }
    }
    func registerMediaRoot(_ url: URL) throws {
        try userAccess { db in
            try photosTables(db)
            try db.execute("INSERT OR IGNORE INTO media_roots VALUES(?)", [url.standardizedFileURL.path])
        }
    }

    private func selectionTable(_ db: SQLDatabase) throws {
        guard try db.rows("SELECT name FROM sqlite_master WHERE name='clip_selection'").isEmpty else { return }
        try db.transaction {
            try db.execute("CREATE TABLE clip_selection(id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)")
            try db.execute("INSERT INTO clip_selection VALUES(1,'[]')")
            try db.execute("CREATE TRIGGER clip_selection_UPDATE AFTER UPDATE ON clip_selection BEGIN UPDATE state SET revision=revision+1,change_token=lower(hex(randomblob(16))) WHERE id=1; END")
            try db.execute("UPDATE state SET revision=revision+1,change_token=lower(hex(randomblob(16))) WHERE id=1")
        }
    }

    func selectedClips() throws -> [SelectedClip] {
        try userAccess { db in
            try selectionTable(db)
            return try JSONDecoder().decode([SelectedClip].self, from: Data(db.scalar("SELECT payload FROM clip_selection WHERE id=1").utf8))
        }
    }

    func saveSelectedClips(_ items: [SelectedClip]) throws {
        try userAccess { db in
            try selectionTable(db)
            let payload = try Self.json(items)
            try db.execute("UPDATE clip_selection SET payload=? WHERE id=1 AND payload != ?", [payload, payload])
        }
    }

    /// Import once, in original event order. Existing JSON history stays intact.
    func migrateAnnotations(from source: URL, identities: [String: String]) throws {
        guard FileManager.default.fileExists(atPath: source.path) else { return }
        let files = try FileManager.default.contentsOfDirectory(at: source, includingPropertiesForKeys: nil)
            .filter { $0.pathExtension == "json" }.sorted { $0.lastPathComponent < $1.lastPathComponent }
        try userAccess { db in
            try db.transaction {
                let imported = Set(try db.rows("SELECT name FROM imported_events").map { $0[0] })
                for file in files where !imported.contains(file.lastPathComponent) {
                    let event = try JSONDecoder().decode(AnnotationEvent.self, from: Data(contentsOf: file))
                    let asset = identities[event.asset] ?? event.asset
                    try Self.write(event.value, asset: asset, updated: event.updated, db: db)
                    try db.execute("INSERT INTO imported_events VALUES(?)", [file.lastPathComponent])
                }
            }
        }
    }

    private static func write(_ value: Annotation, asset: String, updated: Date, db: SQLDatabase) throws {
        let payload = try json(value)
        if try db.rows("SELECT payload FROM annotations WHERE asset=?", [asset]).first?.first == payload { return }
        try db.execute("INSERT INTO annotations VALUES(?,?) ON CONFLICT(asset) DO UPDATE SET payload=excluded.payload", [asset, payload])
        try db.execute("INSERT INTO annotation_history(asset,payload,updated) VALUES(?,?,?)", [asset, payload, ISO8601DateFormatter().string(from: updated)])
    }

    func save(_ value: Annotation, asset: String) throws {
        try userAccess { db in try db.transaction { try Self.write(value, asset: asset, updated: Date(), db: db) } }
    }

    func annotations() throws -> [String: Annotation] {
        try userAccess { db in
            try Dictionary(uniqueKeysWithValues: db.rows("SELECT asset,payload FROM annotations").map {
                ($0[0], try JSONDecoder().decode(Annotation.self, from: Data($0[1].utf8)))
            })
        }
    }

    typealias Snapshot = CatalogSnapshot

    /// A self-contained SQLite dump, never a raw copy of a live database.
    /// The receipt is published last, only after integrity checking and fsync.
    @discardableResult func snapshotIfChanged() throws -> Snapshot {
        try prepareUserStore()
        return try access { db in
            try mirrorUserState(into: db)
            return try SnapshotStore(directory: backups).snapshot(of: db, kind: .fullCatalog)
        }
    }

    @discardableResult func userSnapshotIfChanged(now: Date = Date()) throws -> Snapshot {
        try userAccess { db in
            let store = SnapshotStore(directory: userBackups)
            let saved = try store.snapshot(of: db, kind: .personalData, created: now)
            try store.retainPersonalSnapshots(preserving: saved, now: now)
            return saved
        }
    }

}
