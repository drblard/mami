import Foundation
import SQLite3

/// A scoped SQLite connection. Catalog access owns the process/filesystem lock.
final class SQLDatabase {
    private(set) var handle: OpaquePointer?
    private let path: String

    init(_ url: URL, readOnly: Bool = false, createIfMissing: Bool = true) throws {
        path = url.path
        let flags = readOnly ? SQLITE_OPEN_READONLY : SQLITE_OPEN_READWRITE | (createIfMissing ? SQLITE_OPEN_CREATE : 0)
        guard sqlite3_open_v2(url.path, &handle, flags, nil) == SQLITE_OK else {
            let failure = error()
            sqlite3_close(handle)
            handle = nil
            throw failure
        }
        sqlite3_busy_timeout(handle, 5000)
    }

    deinit { sqlite3_close(handle) }

    func error(operation: String? = nil) -> AppError {
        let context = operation.map { " while \($0)" } ?? ""
        return .message("SQLite \(path)\(context): \(String(cString: sqlite3_errmsg(handle)))")
    }

    func rows(_ sql: String, _ bindings: [String] = []) throws -> [[String]] {
        var statement: OpaquePointer?
        guard sqlite3_prepare_v2(handle, sql, -1, &statement, nil) == SQLITE_OK else { throw error(operation: "preparing \(sql.prefix(160))") }
        defer { sqlite3_finalize(statement) }
        for (index, value) in bindings.enumerated() {
            let result = value.withCString {
                sqlite3_bind_text(statement, Int32(index + 1), $0, -1, unsafeBitCast(-1, to: sqlite3_destructor_type.self))
            }
            guard result == SQLITE_OK else { throw error() }
        }
        var result: [[String]] = []
        while true {
            let step = sqlite3_step(statement)
            if step == SQLITE_DONE { return result }
            guard step == SQLITE_ROW else { throw error(operation: "executing \(sql.prefix(160))") }
            result.append((0..<sqlite3_column_count(statement)).map {
                sqlite3_column_text(statement, $0).map { String(cString: $0) } ?? ""
            })
        }
    }

    func onlyRow(_ sql: String, _ bindings: [String] = []) throws -> [String] {
        let result = try rows(sql, bindings)
        guard result.count == 1 else { throw AppError.message("Expected one database state row, found \(result.count)") }
        return result[0]
    }

    func hasTable(_ name: String) throws -> Bool {
        try !rows("SELECT name FROM sqlite_master WHERE type='table' AND name=?", [name]).isEmpty
    }

    func preserveWALSidecars() throws {
        var enabled: Int32 = 1
        guard sqlite3_file_control(handle, "main", SQLITE_FCNTL_PERSIST_WAL, &enabled) == SQLITE_OK else {
            throw error(operation: "preserving WAL reader sidecars")
        }
    }

    func execute(_ sql: String, _ bindings: [String] = []) throws { _ = try rows(sql, bindings) }
    func scalar(_ sql: String, _ bindings: [String] = []) throws -> String { try rows(sql, bindings).first?.first ?? "" }

    func transaction(_ action: () throws -> Void) throws {
        try execute("BEGIN IMMEDIATE")
        do {
            try action()
            try execute("COMMIT")
        } catch {
            // Cleanup must not replace the original operation's failure.
            try? execute("ROLLBACK")
            throw error
        }
    }

    func backup(to destination: SQLDatabase) throws {
        guard let backup = sqlite3_backup_init(destination.handle, "main", handle, "main") else { throw destination.error() }
        let step = sqlite3_backup_step(backup, -1)
        let finish = sqlite3_backup_finish(backup)
        guard step == SQLITE_DONE, finish == SQLITE_OK else {
            throw AppError.message("SQLite backup failed (step \(step), finish \(finish)): \(destination.error())")
        }
    }
}
