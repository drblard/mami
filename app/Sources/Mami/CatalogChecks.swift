import Foundation
import MamiCore

/// Integration checks run against a fresh directory; all fixtures are retained.
func checkCatalog(at root: URL) throws {
    guard !FileManager.default.fileExists(atPath: root.path) else { throw AppError.message("Catalog check directory must be new") }
    let catalog = Catalog(directory: root.appendingPathComponent("live"))
    func require(_ condition: @autoclosure () throws -> Bool, _ message: String) throws {
        if try !condition() { throw AppError.message(message) }
    }
    let isolatedDirectory = root.appendingPathComponent("configured-fixture")
    let configured = Catalog.configured(environment: ["MAMI_CATALOG": isolatedDirectory.path], home: root)
    try require(configured.backups == isolatedDirectory.appendingPathComponent("backups"), "Catalog override leaked backups into the live library")
    try require(configured.legacyAnnotationsDirectory == isolatedDirectory.appendingPathComponent("legacy-annotations"), "Catalog override reads live legacy annotations")
    try require(configured.artifacts == isolatedDirectory.appendingPathComponent("index-artifacts"), "Catalog override writes live indexing artifacts")
    let production = Catalog.configured(environment: [:], home: root)
    let support = root.appendingPathComponent("Library/Application Support/Mami")
    try require(production.database == support.appendingPathComponent("Derived/Catalog/catalog.sqlite"), "Generated catalog is outside Application Support")
    try require(production.userDatabase == support.appendingPathComponent("Personal/user.sqlite"), "Personal data was mixed into the derived catalog directory")
    try require(production.userBackups == support.appendingPathComponent("Personal/Backups/user-state"), "Personal backups have no persistent production home")
    let configurationRoot = root.appendingPathComponent("configuration-fixture")
    try FileManager.default.createDirectory(at: configurationRoot, withIntermediateDirectories: true)
    let configuration: [String: String] = ["index": "/unused/index", "packed_index": "/live/vectors", "search_projection": "/live/search.sqlite", "pack_previews": "1"]
    try JSONEncoder().encode(configuration).write(to: configurationRoot.appendingPathComponent("configuration.json"))
    let isolatedConfiguration = try Configuration.load(environment: ["MAMI_CATALOG": isolatedDirectory.path], resourceDirectory: configurationRoot)
    try require(isolatedConfiguration.packedIndex == nil && isolatedConfiguration.searchProjection == nil && !isolatedConfiguration.packPreviews,
                "Isolated catalog inherited writable production search/cache paths")
    let pendingPayload: [String: Any] = [
        "path": "library:sha256:pending", "kind": "video", "url": "file:///original/pending.mp4",
        "frames": [], "match": ["path": "library:sha256:pending", "kind": "video", "timestamp": 0, "frame": ""],
        "metadata": NSNull(), "assetID": "sha256:pending", "previewState": "pending"
    ]
    let pendingMedia = try JSONDecoder().decode(Media.self, from: JSONSerialization.data(withJSONObject: pendingPayload))
    try require(pendingMedia.frames.isEmpty && pendingMedia.previewState == "pending" && pendingMedia.url.isFileURL,
                "Imported playable media requires preview frames")
    let frame = Sample(path: "example.mp4", kind: "video", timestamp: 0, frame: "/cache/example.jpg", score: nil, evidence: nil)
    let item = Media(path: "example.mp4", kind: "video", url: URL(fileURLWithPath: "/original/example.mp4"), frames: [frame], match: frame, metadata: nil, assetID: "sha256:example")
    try catalog.synchronize([item])
    let cacheFixture = try SQLDatabase(catalog.database)
    try cacheFixture.execute("CREATE TABLE future_generated_cache(payload TEXT)")
    try cacheFixture.execute("INSERT INTO future_generated_cache VALUES('regenerable')")
    try require(catalog.media(forAssetIDs: []).isEmpty, "Empty selection unexpectedly loaded media")
    try require(catalog.media(forAssetIDs: [item.assetID, item.assetID, "missing"]).map(\.path) == [item.path], "Scoped media lookup did not deduplicate or filter assets")
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
    try require(catalog.media(forAssetIDs: [item.assetID]).first?.url == relocated.url, "Scoped selection lookup lost relocation")
    try require(catalog.annotations()[item.assetID]?.place == label.place, "Relocation lost labels")
    let relocatedSnapshot = try catalog.snapshotIfChanged()
    try catalog.synchronize([relocated])
    try require(catalog.snapshotIfChanged().file == relocatedSnapshot.file, "Repeated relocation created an unnecessary backup")
    let personal = try catalog.userSnapshotIfChanged()
    let personalURL = catalog.userBackups.appendingPathComponent(personal.file)
    let personalDB = try SQLDatabase(personalURL, readOnly: true)
    try require(personalDB.scalar("SELECT count(*) FROM sqlite_master WHERE name IN ('media','index_units','index_jobs','scan_files','future_generated_cache')") == "0", "Personal backup contains generated data")
    try catalog.synchronize([item])
    try require(catalog.userSnapshotIfChanged().file == personal.file, "Metadata/index changes triggered a personal backup")
    let personalRestore = Catalog(directory: root.appendingPathComponent("personal-restored"))
    try FileManager.default.createDirectory(at: personalRestore.directory, withIntermediateDirectories: true)
    try FileManager.default.copyItem(at: personalURL, to: personalRestore.userDatabase)
    try require(personalRestore.annotations()[item.assetID]?.place == label.place, "Personal-only restore lost edits")
    try require(personalRestore.media().isEmpty, "Personal-only restore unexpectedly contained cached media")
    try personalRestore.synchronize([item])
    try require(personalRestore.annotations()[item.assetID]?.place == label.place, "Rebuilt media did not reconnect personal data")
    let missingPersonal = Catalog(directory: root.appendingPathComponent("missing-personal"))
    try FileManager.default.createDirectory(at: missingPersonal.directory, withIntermediateDirectories: true)
    try FileManager.default.copyItem(at: catalog.database, to: missingPersonal.database)
    failed = false
    do { _ = try missingPersonal.annotations() } catch { failed = true }
    try require(failed, "Missing personal DB silently regenerated stale edits")
    try FileManager.default.createDirectory(at: personalRestore.userBackups, withIntermediateDirectories: true)
    let unrecognized = Catalog.Snapshot(identity: personal.identity, revision: "unknown", changeToken: "unknown", file: "catalog-unrecognized.sqlite", created: .distantPast)
    try Data("unrecognized retained file".utf8).write(to: personalRestore.userBackups.appendingPathComponent(unrecognized.file), options: .withoutOverwriting)
    try JSONEncoder().encode(unrecognized).write(to: personalRestore.userBackups.appendingPathComponent("unrecognized.json"), options: .withoutOverwriting)
    let policy = BackupRetentionPolicy.standard
    let excessSnapshotsToExercisePruning = 16
    let generatedSnapshotCount = policy.recentSnapshotCount + excessSnapshotsToExercisePruning
    let fixedRetentionStart = ISO8601DateFormatter().date(from: "2026-09-28T12:15:00Z")!
    var generatedSnapshots: [Catalog.Snapshot] = []
    for i in 0..<generatedSnapshotCount {
        try personalRestore.save(Annotation(favorite: true, tags: ["retention-\(i)"], place: "kept"), asset: item.assetID)
        // All fixture timestamps are distinct and inside one UTC bucket,
        // regardless of the real clock or the configured recent-count limit.
        let timestamp = fixedRetentionStart.addingTimeInterval(Double(i) / Double(generatedSnapshotCount))
        generatedSnapshots.append(try personalRestore.userSnapshotIfChanged(now: timestamp))
    }
    let retained = try FileManager.default.contentsOfDirectory(at: personalRestore.userBackups, includingPropertiesForKeys: nil).filter { $0.pathExtension == "sqlite" }
    let expectedFiles = Set(generatedSnapshots.suffix(policy.recentSnapshotCount).map(\.file)).union([unrecognized.file])
    try require(Set(retained.map(\.lastPathComponent)) == expectedFiles, "Personal backup retention kept the wrong snapshots")
    try require(FileManager.default.fileExists(atPath: personalRestore.userBackups.appendingPathComponent(unrecognized.file).path), "Retention removed an unrecognized backup")
    try checkProjection(at: root)
    print("USER STORE TEST PASSED: verified migration, generated-data exclusion, index-change suppression, personal-only restore, content-ID reconnect, missing-store detection and bounded retention")
    try checkPeopleStore(at: root)
    print("CATALOG TEST PASSED: no-change skips, no-op saves, legacy migration, rollback, restore, backup failure/retry and retained history")
    print("CATALOG TEST OUTPUT: \(root.path)")
}
