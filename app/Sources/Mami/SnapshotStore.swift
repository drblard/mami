import Foundation
import Darwin
import MamiCore

enum DurableFile {
    static func synchronize(_ url: URL) throws {
        let file = try FileHandle(forWritingTo: url)
        defer { try? file.close() }
        try file.synchronize()
    }

    static func synchronizeDirectory(_ url: URL) throws {
        let descriptor = Darwin.open(url.path, O_RDONLY)
        guard descriptor >= 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
        defer { Darwin.close(descriptor) }
        guard fsync(descriptor) == 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
    }
}

/// Publishes verified SQLite snapshots and receipts. The caller holds the writer lock.
struct SnapshotStore {
    enum Kind { case fullCatalog, personalData }
    let directory: URL

    private func safeComponent(_ value: String) -> Bool {
        !value.isEmpty && value.unicodeScalars.allSatisfy {
            CharacterSet.alphanumerics.contains($0) || $0 == "-" || $0 == "_"
        }
    }

    private func validFilename(_ name: String) -> Bool {
        URL(fileURLWithPath: name).lastPathComponent == name && name.hasPrefix("catalog-") && name.hasSuffix(".sqlite")
    }

    private func isRegularFile(_ url: URL) -> Bool {
        guard let values = try? url.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey]) else { return false }
        return values.isRegularFile == true && values.isSymbolicLink != true
    }

    func snapshot(of database: SQLDatabase, kind: Kind, created: Date = Date()) throws -> CatalogSnapshot {
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let state = try database.onlyRow("SELECT identity,revision,change_token FROM state WHERE id=1")
        guard state.count == 3, state.allSatisfy(safeComponent) else { throw AppError.message("Invalid catalog snapshot identity") }
        let receipt = directory.appendingPathComponent("\(state[0])-r\(state[1])-\(state[2]).json")
        if FileManager.default.fileExists(atPath: receipt.path) {
            guard isRegularFile(receipt) else { throw AppError.message("Snapshot receipt is not a regular file") }
            let existing = try JSONDecoder().decode(CatalogSnapshot.self, from: Data(contentsOf: receipt))
            guard existing.identity == state[0], existing.revision == state[1], existing.changeToken == state[2],
                  validFilename(existing.file), isRegularFile(directory.appendingPathComponent(existing.file)) else {
                throw AppError.message("Snapshot receipt or its database does not match the requested state")
            }
            return existing
        }

        let name = "catalog-\(state[0])-r\(state[1])-\(UUID().uuidString).sqlite"
        let destination = directory.appendingPathComponent(name)
        try Data().write(to: destination, options: .withoutOverwriting)
        do {
            let copy = try SQLDatabase(destination)
            try copy.execute("PRAGMA journal_mode=PERSIST")
            try database.backup(to: copy)
            if kind == .fullCatalog {
                // Explicit full exports can seed a new separate personal store.
                for row in try copy.rows("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'user_owned_%'") {
                    try copy.execute("DROP TRIGGER \(row[0])")
                }
                try copy.execute("DROP TABLE IF EXISTS user_store_migration")
                if try copy.hasTable("index_schema"), try copy.scalar("SELECT version FROM index_schema") == "2" {
                    // A full compatibility export can resume the original
                    // single-worker pipeline from the same frame/AI checkpoints.
                    try copy.execute("DROP TRIGGER IF EXISTS index_jobs_preview")
                    try copy.execute("DROP TRIGGER IF EXISTS index_jobs_preview_time")
                    try copy.execute("DROP TABLE IF EXISTS preview_jobs")
                    try copy.execute("DROP TABLE IF EXISTS preview_control")
                    try copy.execute("UPDATE index_schema SET version=1")
                }
            }
            guard try copy.scalar("PRAGMA integrity_check") == "ok" else { throw AppError.message("Snapshot failed integrity checking") }
        }
        try DurableFile.synchronize(destination)
        let snapshot = CatalogSnapshot(identity: state[0], revision: state[1], changeToken: state[2], file: name, created: created)
        let pending = directory.appendingPathComponent(name + ".receipt-pending")
        try JSONEncoder().encode(snapshot).write(to: pending, options: .withoutOverwriting)
        try DurableFile.synchronize(pending)
        try FileManager.default.linkItem(at: pending, to: receipt)
        try DurableFile.synchronizeDirectory(directory)
        return snapshot
    }

    func retainPersonalSnapshots(preserving current: CatalogSnapshot, now: Date) throws {
        let files = try FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: [.isRegularFileKey, .isSymbolicLinkKey])
        let receipts: [(URL, CatalogSnapshot)] = files.compactMap { url in
            guard url.pathExtension == "json", isRegularFile(url),
                  let data = try? Data(contentsOf: url), let value = try? JSONDecoder().decode(CatalogSnapshot.self, from: data),
                  value.identity == current.identity, validFilename(value.file),
                  isRegularFile(directory.appendingPathComponent(value.file)) else { return nil }
            return (url, value)
        }
        let retained = BackupRetentionPolicy.standard.retainedFiles(in: receipts.map { $0.1 }, preserving: current, now: now)
        let latest = try SQLDatabase(directory.appendingPathComponent(current.file), readOnly: true)
        guard try latest.scalar("PRAGMA quick_check") == "ok",
              try latest.scalar("SELECT version FROM user_store_info") == "1",
              try latest.scalar("SELECT change_token FROM state WHERE id=1") == current.changeToken else {
            throw AppError.message("Personal backup verification failed; older snapshots retained")
        }
        for (receipt, snapshot) in receipts where !retained.contains(snapshot.file) {
            let target = directory.appendingPathComponent(snapshot.file)
            guard let candidate = try? SQLDatabase(target, readOnly: true),
                  (try? candidate.scalar("SELECT version FROM user_store_info")) == "1",
                  (try? candidate.scalar("SELECT identity FROM state WHERE id=1")) == snapshot.identity,
                  (try? candidate.scalar("SELECT change_token FROM state WHERE id=1")) == snapshot.changeToken else { continue }
            try FileManager.default.removeItem(at: receipt)
            for suffix in [".receipt-pending", "-journal"] {
                let extra = directory.appendingPathComponent(snapshot.file + suffix)
                if FileManager.default.fileExists(atPath: extra.path) { try FileManager.default.removeItem(at: extra) }
            }
            try FileManager.default.removeItem(at: target)
        }
    }
}
