import Foundation
import SQLite3

var database: OpaquePointer?
guard sqlite3_open_v2(CommandLine.arguments[1], &database, SQLITE_OPEN_READONLY, nil) == SQLITE_OK else { fatalError("Cannot open fixture") }
defer { sqlite3_close(database) }
for sql in [
    "PRAGMA user_version",
    "SELECT DISTINCT camera FROM files ORDER BY camera",
    "SELECT min(captured) FROM files WHERE captured != ''",
    "SELECT captured FROM files WHERE captured > '' ORDER BY captured LIMIT 1",
    "SELECT count(*) FROM files",
    "SELECT coalesce(max(sequence),0) FROM vector_events",
    "SELECT asset,captured,summary FROM files WHERE 1 ORDER BY captured DESC,asset LIMIT 100"
] {
    let started = CFAbsoluteTimeGetCurrent()
    var statement: OpaquePointer?
    guard sqlite3_prepare_v2(database, sql, -1, &statement, nil) == SQLITE_OK else { fatalError(String(cString: sqlite3_errmsg(database))) }
    var rows = 0
    var status = sqlite3_step(statement)
    while status == SQLITE_ROW { rows += 1; status = sqlite3_step(statement) }
    sqlite3_finalize(statement)
    guard status == SQLITE_DONE else { fatalError(String(cString: sqlite3_errmsg(database))) }
    let result: [String: Any] = ["sql": sql, "rows": rows, "ms": (CFAbsoluteTimeGetCurrent()-started)*1000]
    print(String(decoding: try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]), as: UTF8.self))
}
