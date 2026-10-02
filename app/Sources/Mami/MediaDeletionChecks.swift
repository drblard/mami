import Foundation
import CryptoKit

/// Moves an isolated fixture through the real Trash, then removes only that fixture
/// from the Trash. A rejected deletion must put its original back.
func checkMediaDeletion(at root: URL) async throws {
    guard !FileManager.default.fileExists(atPath: root.path) else { throw AppError.message("Media deletion check directory must be new") }
    func require(_ condition: @autoclosure () throws -> Bool, _ message: String) throws {
        if try !condition() { throw AppError.message(message) }
    }
    let originals = root.appendingPathComponent("originals")
    try FileManager.default.createDirectory(at: originals, withIntermediateDirectories: true)
    let catalog = Catalog(directory: root.appendingPathComponent("catalog"))
    try catalog.prepareUserStore()
    func fixture(_ name: String, _ contents: String, asset: String? = nil) throws -> Media {
        let url = originals.appendingPathComponent(name)
        try Data(contents.utf8).write(to: url, options: .withoutOverwriting)
        let digest = SHA256.hash(data: Data(contents.utf8)).map { String(format: "%02x", $0) }.joined()
        let frame = Sample(path: "library:sha256:\(digest)", kind: "video", timestamp: 0, frame: "", score: nil, evidence: nil)
        return Media(path: frame.path, kind: "video", url: url, frames: [frame], match: frame, metadata: nil, assetID: asset ?? "sha256:\(digest)")
    }
    let take = try fixture("take1.mp4", "media deletion check: first take")
    let kept = try fixture("take2.mp4", "media deletion check: second take")
    let rejected = try fixture("take3.mp4", "media deletion check: rejected", asset: "sha256:not-a-content-identity")
    try catalog.synchronize([take, kept, rejected])
    let configuration = try Configuration.load()

    let outcome = try await MediaDeletion.moveToTrash([take], catalog: catalog, configuration: configuration)
    guard let location = outcome.trashed[take.url] else { throw AppError.message("Deleted original was not moved to the Trash") }
    defer { try? FileManager.default.removeItem(at: location) }
    try require(outcome.assets == [take.assetID] && outcome.trashed.count == 1, "Deletion outcome named other media")
    try require(!FileManager.default.fileExists(atPath: take.url.path), "Original remained in the library folder")
    try require(try Data(contentsOf: location) == Data("media deletion check: first take".utf8), "Trashed original changed")
    try require(try catalog.media().map(\.assetID).sorted() == [kept.assetID, rejected.assetID].sorted(), "Deleted media remained in the catalog")
    let personal = try SQLDatabase(catalog.userDatabase)
    try require(try personal.rows("SELECT asset FROM deleted_media") == [[take.assetID]], "Deletion was not recorded in personal data")

    do {
        _ = try await MediaDeletion.moveToTrash([rejected], catalog: catalog, configuration: configuration)
        throw AppError.message("Invalid content identity was deleted")
    } catch let error as AppError where "\(error)".contains("Could not move media to the Trash") && "\(error)".contains("Nothing was deleted") { }
    try require(FileManager.default.fileExists(atPath: rejected.url.path), "Rejected deletion did not put its original back")
    try require(try catalog.media().count == 2, "Rejected deletion changed the catalog")
    let result = ["status": "passed", "trashed": location.lastPathComponent]
    try JSONSerialization.data(withJSONObject: result).write(to: root.appendingPathComponent("result.json"))
    print("MEDIA DELETION TEST PASSED: trashed, forgotten, recorded; rejected deletion restored")
}
