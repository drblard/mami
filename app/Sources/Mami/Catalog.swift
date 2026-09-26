import Foundation
import SQLite3
import Darwin

/// All catalog access (including backups) shares a process and filesystem lock.
/// No network or source-media writes are involved.
struct Catalog: Sendable {
    static let standard = Catalog(directory: ProcessInfo.processInfo.environment["MAMI_CATALOG"].map { URL(fileURLWithPath: $0) }
                                  ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("mami-lab/catalog/database"),
                                  backups: FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("mami-lab/backups/catalog"))
    private static let lock = NSRecursiveLock()
    let directory: URL
    let backups: URL
    var database: URL { directory.appendingPathComponent("catalog.sqlite") }

    init(directory: URL, backups: URL? = nil) {
        self.directory = directory
        self.backups = backups ?? directory.appendingPathComponent("backups")
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
                try db.execute("INSERT INTO state VALUES(1, ?, 0)", [UUID().uuidString])
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

    private func photosTables(_ db: SQLDatabase) throws {
        try db.execute("CREATE TABLE IF NOT EXISTS photos_import_history(resource TEXT PRIMARY KEY, digest TEXT NOT NULL, size TEXT NOT NULL)")
        try db.execute("CREATE TABLE IF NOT EXISTS media_roots(path TEXT PRIMARY KEY)")
        for table in ["photos_import_history", "media_roots"] {
            try db.execute("CREATE TRIGGER IF NOT EXISTS \(table)_INSERT AFTER INSERT ON \(table) BEGIN UPDATE state SET revision=revision+1,change_token=lower(hex(randomblob(16))) WHERE id=1; END")
        }
    }
    func photosHistory() throws -> Set<String> {
        try access { db in
            try photosTables(db)
            return Set(try db.rows("SELECT resource FROM photos_import_history").map { $0[0] })
        }
    }
    func recordPhotosImport(resource: String, digest: String, size: Int64) throws {
        try access { db in
            try photosTables(db)
            try db.execute("INSERT OR IGNORE INTO photos_import_history VALUES(?,?,?)", [resource, digest, String(size)])
        }
    }
    func registerMediaRoot(_ url: URL) throws {
        try access { db in
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
        try access { db in
            try selectionTable(db)
            return try JSONDecoder().decode([SelectedClip].self, from: Data(db.scalar("SELECT payload FROM clip_selection WHERE id=1").utf8))
        }
    }

    func saveSelectedClips(_ items: [SelectedClip]) throws {
        try access { db in
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
        try access { db in
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
        try access { db in try db.transaction { try Self.write(value, asset: asset, updated: Date(), db: db) } }
    }

    func annotations() throws -> [String: Annotation] {
        try access { db in
            try Dictionary(uniqueKeysWithValues: db.rows("SELECT asset,payload FROM annotations").map {
                ($0[0], try JSONDecoder().decode(Annotation.self, from: Data($0[1].utf8)))
            })
        }
    }

    struct Snapshot: Codable, Sendable {
        let identity: String
        let revision: String
        let changeToken: String
        let file: String
        let created: Date
    }

    /// A self-contained SQLite dump, never a raw copy of a live database.
    /// The receipt is published last, only after integrity checking and fsync.
    @discardableResult func snapshotIfChanged() throws -> Snapshot {
        try access { db in
            try FileManager.default.createDirectory(at: backups, withIntermediateDirectories: true)
            let state = try db.rows("SELECT identity,revision,change_token FROM state")[0]
            let receipt = backups.appendingPathComponent("\(state[0])-r\(state[1])-\(state[2]).json")
            if FileManager.default.fileExists(atPath: receipt.path) {
                let existing = try JSONDecoder().decode(Snapshot.self, from: Data(contentsOf: receipt))
                if FileManager.default.fileExists(atPath: backups.appendingPathComponent(existing.file).path) {
                    guard existing.identity == state[0], existing.revision == state[1], existing.changeToken == state[2] else {
                        throw AppError.message("Catalog backup receipt does not match its revision")
                    }
                    return existing
                }
                throw AppError.message("Catalog snapshot receipt exists but its database is missing: \(existing.file)")
            }
            let name = "catalog-\(state[0])-r\(state[1])-\(UUID().uuidString).sqlite"
            let destination = backups.appendingPathComponent(name)
            guard FileManager.default.createFile(atPath: destination.path, contents: nil) else {
                throw AppError.message("Cannot create catalog snapshot")
            }
            do {
                let copy = try SQLDatabase(destination)
                try copy.execute("PRAGMA journal_mode=PERSIST")
                guard let backup = sqlite3_backup_init(copy.handle, "main", db.handle, "main") else { throw copy.error() }
                let result = sqlite3_backup_step(backup, -1)
                let finish = sqlite3_backup_finish(backup)
                guard result == SQLITE_DONE, finish == SQLITE_OK else { throw copy.error() }
                guard try copy.scalar("PRAGMA integrity_check") == "ok" else { throw AppError.message("Catalog snapshot failed integrity check") }
            }
            let file = try FileHandle(forWritingTo: destination)
            defer { try? file.close() }
            try file.synchronize()
            let snapshot = Snapshot(identity: state[0], revision: state[1], changeToken: state[2], file: name, created: Date())
            let pending = backups.appendingPathComponent(name + ".receipt-pending")
            try JSONEncoder().encode(snapshot).write(to: pending, options: .withoutOverwriting)
            let receiptFile = try FileHandle(forWritingTo: pending)
            try receiptFile.synchronize()
            try receiptFile.close()
            // An atomic hard-link publishes the complete receipt; even staging
            // files are retained to honor the lab's preserve-all-outputs rule.
            try FileManager.default.linkItem(at: pending, to: receipt)
            let folder = Darwin.open(backups.path, O_RDONLY)
            if folder >= 0 { _ = fsync(folder); Darwin.close(folder) }
            return snapshot
        }
    }
}

/// Thin SQLite wrapper. Connections are scoped to Catalog.access's lock.
final class SQLDatabase {
    var handle: OpaquePointer?
    init(_ url: URL, readOnly: Bool = false) throws {
        let flags = readOnly ? SQLITE_OPEN_READONLY : SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE
        guard sqlite3_open_v2(url.path, &handle, flags, nil) == SQLITE_OK else {
            let failure = error()
            sqlite3_close(handle)
            handle = nil
            throw failure
        }
        sqlite3_busy_timeout(handle, 5000)
    }
    deinit { sqlite3_close(handle) }
    func error() -> AppError { .message(String(cString: sqlite3_errmsg(handle))) }
    func rows(_ sql: String, _ bindings: [String] = []) throws -> [[String]] {
        var statement: OpaquePointer?
        guard sqlite3_prepare_v2(handle, sql, -1, &statement, nil) == SQLITE_OK else { throw error() }
        defer { sqlite3_finalize(statement) }
        for (index, value) in bindings.enumerated() {
            let result = value.withCString { sqlite3_bind_text(statement, Int32(index + 1), $0, -1, unsafeBitCast(-1, to: sqlite3_destructor_type.self)) }
            guard result == SQLITE_OK else { throw error() }
        }
        var result: [[String]] = []
        while true {
            let step = sqlite3_step(statement)
            if step == SQLITE_DONE { return result }
            guard step == SQLITE_ROW else { throw error() }
            result.append((0..<sqlite3_column_count(statement)).map {
                sqlite3_column_text(statement, $0).map { String(cString: $0) } ?? ""
            })
        }
    }
    func execute(_ sql: String, _ bindings: [String] = []) throws { _ = try rows(sql, bindings) }
    func scalar(_ sql: String) throws -> String { try rows(sql).first?.first ?? "" }
    func transaction(_ action: () throws -> Void) throws {
        try execute("BEGIN IMMEDIATE")
        do { try action(); try execute("COMMIT") }
        catch { try? execute("ROLLBACK"); throw error }
    }
}

@MainActor final class CatalogBackups: ObservableObject {
    static let shared = CatalogBackups()
    @Published private(set) var status = "Catalog backup pending"
    @Published private(set) var error: String?
    private var task: Task<Void, Never>?

    func schedule() {
        task?.cancel()
        task = Task {
            do {
                try await Task.sleep(for: .seconds(2))
                let snapshot = try await Task.detached { try Catalog.standard.snapshotIfChanged() }.value
                try Task.checkCancellation()
                status = "Catalog backed up · " + snapshot.created.formatted(date: .abbreviated, time: .shortened)
                error = nil
            } catch is CancellationError { }
            catch { self.error = "Catalog backup failed: \(error.localizedDescription)" }
        }
    }

    /// Flush pending edits on orderly application termination; restart retries
    /// any revision left without a completed receipt by an unexpected exit.
    func flush() {
        task?.cancel()
        do { _ = try Catalog.standard.snapshotIfChanged(); error = nil }
        catch { self.error = "Catalog backup failed: \(error.localizedDescription)" }
    }
}
