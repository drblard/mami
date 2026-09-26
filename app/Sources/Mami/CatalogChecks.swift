import Foundation

/// Integration checks run against a fresh directory; all fixtures are retained.
func checkCatalog(at root: URL) throws {
    guard !FileManager.default.fileExists(atPath: root.path) else { throw AppError.message("Catalog check directory must be new") }
    let catalog = Catalog(directory: root.appendingPathComponent("live"))
    func require(_ condition: @autoclosure () throws -> Bool, _ message: String) throws {
        if try !condition() { throw AppError.message(message) }
    }
    let frame = Sample(path: "example.mp4", kind: "video", timestamp: 0, frame: "/cache/example.jpg", score: nil, evidence: nil)
    let item = Media(path: "example.mp4", kind: "video", url: URL(fileURLWithPath: "/original/example.mp4"), frames: [frame], match: frame, metadata: nil, assetID: "sha256:example")
    try catalog.synchronize([item])
    let original = try catalog.snapshotIfChanged()
    let before = try FileManager.default.contentsOfDirectory(atPath: catalog.backups.path).sorted()
    let originalURL = catalog.backups.appendingPathComponent(original.file)
    let modified = try originalURL.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate
    try catalog.synchronize([item])
    _ = try catalog.media()
    _ = try catalog.annotations()
    let unchanged = try catalog.snapshotIfChanged()
    try require(original.file == unchanged.file, "Unchanged catalog created another dump")
    try require(before == FileManager.default.contentsOfDirectory(atPath: catalog.backups.path).sorted(), "Idle catalog wrote backup files")
    try require(modified == originalURL.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate, "Idle catalog rewrote its dump")

    let legacy = root.appendingPathComponent("legacy")
    try FileManager.default.createDirectory(at: legacy, withIntermediateDirectories: true)
    let label = Annotation(favorite: true, tags: ["Grădină", "family"], place: "Bunici")
    let event = AnnotationEvent(asset: item.url.path, updated: Date(), value: label)
    try JSONEncoder().encode(event).write(to: legacy.appendingPathComponent("001.json"), options: .withoutOverwriting)
    try catalog.migrateAnnotations(from: legacy, identities: [item.url.path: item.assetID])
    try require(catalog.annotations()[item.assetID] == label, "Legacy path-keyed labels were not migrated")
    let changed = try catalog.snapshotIfChanged()
    try require(changed.file != original.file, "Changed catalog did not produce a dump")
    try catalog.migrateAnnotations(from: legacy, identities: [item.url.path: item.assetID])
    try catalog.save(label, asset: item.assetID)
    try require(catalog.snapshotIfChanged().file == changed.file, "Repeated migration/no-op save created a dump")

    // Restore uses only the self-contained snapshot, not a journal or live DB.
    let restore = Catalog(directory: root.appendingPathComponent("restored"))
    try FileManager.default.createDirectory(at: restore.directory, withIntermediateDirectories: true)
    try FileManager.default.copyItem(at: catalog.backups.appendingPathComponent(changed.file), to: restore.database)
    try require(restore.annotations()[item.assetID] == label, "Snapshot restore lost labels")
    try require(restore.media().first?.assetID == item.assetID, "Snapshot restore lost media")
    let restoredDB = try SQLDatabase(restore.database, readOnly: true)
    try require(restoredDB.scalar("PRAGMA integrity_check") == "ok", "Restored snapshot is corrupt")
    try require(restoredDB.scalar("SELECT count(*) FROM annotation_history") == "1", "Snapshot restore lost annotation history")

    // A later corrupt import must roll back the entire batch, including its
    // history and revision. The valid fixture and corrupt file both remain.
    let other = AnnotationEvent(asset: "other", updated: Date(), value: label)
    try JSONEncoder().encode(other).write(to: legacy.appendingPathComponent("002.json"), options: .withoutOverwriting)
    try Data("invalid".utf8).write(to: legacy.appendingPathComponent("003.json"), options: .withoutOverwriting)
    var failed = false
    do { try catalog.migrateAnnotations(from: legacy, identities: [:]) } catch { failed = true }
    try require(failed, "Corrupt legacy event unexpectedly imported")
    try require(catalog.annotations()["other"] == nil, "Failed import partially committed")
    try require(catalog.snapshotIfChanged().file == changed.file, "Rolled-back import changed backup revision")

    try catalog.save(Annotation(favorite: false, tags: label.tags, place: label.place), asset: item.assetID)
    let blocked = root.appendingPathComponent("not-a-directory")
    try Data("retained failure fixture".utf8).write(to: blocked, options: .withoutOverwriting)
    failed = false
    do { _ = try Catalog(directory: catalog.directory, backups: blocked).snapshotIfChanged() } catch { failed = true }
    try require(failed, "Unwritable backup destination did not report failure")
    let retried = try catalog.snapshotIfChanged()
    try require(retried.file != changed.file, "Failed backup prevented retry")
    try require(catalog.annotations()[item.assetID]?.favorite == false, "Backup failure lost committed changes")
    try require(FileManager.default.fileExists(atPath: originalURL.path), "Earlier backup disappeared")
    // Branch from the restored historical snapshot into the same backup folder.
    // Its numeric revision equals the live DB's, but its contents differ.
    let branch = Catalog(directory: restore.directory, backups: catalog.backups)
    try branch.save(Annotation(favorite: true, tags: ["restored branch"], place: "Elsewhere"), asset: item.assetID)
    let branchSnapshot = try branch.snapshotIfChanged()
    try require(branchSnapshot.revision == retried.revision, "Restore-branch fixture did not reach matching revisions")
    try require(branchSnapshot.file != retried.file, "Restored branch reused an unrelated revision dump")
    try require(branchSnapshot.changeToken != retried.changeToken, "Restored branch reused a change token")
    let relocated = Media(path: item.path, kind: item.kind, url: URL(fileURLWithPath: "/new/2026-09-18/renamed.mp4"),
                          frames: item.frames, match: item.match, metadata: item.metadata, assetID: item.assetID)
    try catalog.synchronize([relocated])
    try require(catalog.media().count == 1, "Relocation duplicated a catalog entry")
    try require(catalog.media().first?.url == relocated.url, "Relocation did not update the original URL")
    try require(catalog.annotations()[item.assetID]?.place == label.place, "Relocation lost labels")
    let relocatedSnapshot = try catalog.snapshotIfChanged()
    try catalog.synchronize([relocated])
    try require(catalog.snapshotIfChanged().file == relocatedSnapshot.file, "Repeated relocation created an unnecessary backup")
    print("CATALOG TEST PASSED: no-change skips, no-op saves, legacy migration, rollback, restore, backup failure/retry and retained history")
    print("CATALOG TEST OUTPUT: \(root.path)")
}
