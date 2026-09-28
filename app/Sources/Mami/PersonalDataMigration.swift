import Foundation

/// Explicit personal-data boundary. New generated tables never enter this copy.
enum PersonalDataMigration {
    static let version = 1
    static let tables = ["annotations", "annotation_history", "imported_events", "clip_selection", "photos_import_history", "media_roots"]
    private static let requiredTables: Set<String> = ["annotations", "annotation_history", "imported_events"]

    /// Caller holds the catalog writer lock. SQLite copies values without a
    /// string/JSON round-trip; the final path appears only after verification.
    static func create(from source: URL, at destination: URL, identity: String) throws {
        let temporary = destination.deletingLastPathComponent().appendingPathComponent("user-migration-\(UUID().uuidString).sqlite")
        try Data().write(to: temporary, options: .withoutOverwriting)
        do {
            let copy = try SQLDatabase(temporary)
            try copy.execute("ATTACH DATABASE ? AS legacy", [source.path])
            defer { try? copy.execute("DETACH DATABASE legacy") }
            try copy.transaction {
                try copy.execute("CREATE TABLE state(id INTEGER PRIMARY KEY CHECK(id=1), identity TEXT NOT NULL, revision INTEGER NOT NULL, change_token TEXT NOT NULL)")
                try copy.execute("INSERT INTO state VALUES(1,?,0,lower(hex(randomblob(16))))", [identity])
                for table in tables {
                    guard let schema = try copy.rows("SELECT sql FROM legacy.sqlite_master WHERE type='table' AND name=?", [table]).first?.first else {
                        if requiredTables.contains(table) { throw AppError.message("Missing personal-data table: \(table)") }
                        continue
                    }
                    try copy.execute(schema)
                    try copy.execute("INSERT INTO main.\(table) SELECT * FROM legacy.\(table)")
                    let missing = try copy.scalar("SELECT count(*) FROM (SELECT * FROM legacy.\(table) EXCEPT SELECT * FROM main.\(table))")
                    let extra = try copy.scalar("SELECT count(*) FROM (SELECT * FROM main.\(table) EXCEPT SELECT * FROM legacy.\(table))")
                    guard missing == "0", extra == "0",
                          try copy.scalar("SELECT count(*) FROM main.\(table)") == copy.scalar("SELECT count(*) FROM legacy.\(table)") else {
                        throw AppError.message("Personal-data migration changed \(table)")
                    }
                    for row in try copy.rows("SELECT name,sql FROM legacy.sqlite_master WHERE type IN ('index','trigger') AND tbl_name=? AND sql IS NOT NULL", [table]) {
                        if !row[0].hasPrefix("user_owned_") { try copy.execute(row[1]) }
                    }
                }
                try copy.execute("CREATE TABLE user_store_info(version INTEGER NOT NULL, source_identity TEXT NOT NULL)")
                try copy.execute("INSERT INTO user_store_info VALUES(?,?)", [String(version), identity])
                try copy.execute("PRAGMA user_version=\(version)")
            }
            guard try copy.scalar("PRAGMA integrity_check") == "ok" else { throw AppError.message("Personal-data migration failed integrity checking") }
        }
        try DurableFile.synchronize(temporary)
        try FileManager.default.moveItem(at: temporary, to: destination)
        try DurableFile.synchronizeDirectory(destination.deletingLastPathComponent())
    }
}
