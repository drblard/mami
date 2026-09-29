import Foundation

/// Personal people/label edits: exact rows after each change and after undo,
/// revision bumps for backups, one confirmation per face and merge/delete scope.
func checkPeopleStore(at root: URL) throws {
    let catalog = Catalog(directory: root.appendingPathComponent("people-live"))
    func require(_ condition: @autoclosure () throws -> Bool, _ message: String) throws {
        if try !condition() { throw AppError.message("PEOPLE: " + message) }
    }
    let fixed = Date(timeIntervalSince1970: 1_790_000_000)
    let son = Person(id: "person-son", name: "Our son", created: "2026-09-29T00:00:00Z")
    let daughter = Person(id: "person-daughter", name: "Our daughter", created: "2026-09-29T00:00:00Z")
    let photoFace = FaceRef(asset: "sha256:photo", timestamp: nil, box: [0.1, 0.2, 0.3, 0.4])
    let videoFace = FaceRef(asset: "sha256:video", timestamp: 12.5, box: [0.5, 0.1, 0.7, 0.35])
    let otherFace = FaceRef(asset: "sha256:other", timestamp: nil, box: [0.2, 0.2, 0.4, 0.5])
    func labels() throws -> Set<String> {
        try catalog.userAccess { db in
            Set(try db.rows("SELECT face_key,person,verdict FROM face_labels").map { $0.joined(separator: "#") })
        }
    }
    func revision() throws -> Int { try catalog.userAccess { Int(try $0.scalar("SELECT revision FROM state WHERE id=1")) ?? -1 } }
    try require(try catalog.people().isEmpty, "new store has people")
    let start = try revision()
    let created = try catalog.apply(.create(son), now: fixed)
    try catalog.apply(.create(daughter), now: fixed)
    try require(try revision() > start, "people edits must bump the personal revision for backups")
    let confirmed = try catalog.apply(.confirm([photoFace, videoFace], as: son.id), now: fixed)
    try catalog.apply(.reject([otherFace], from: son.id), now: fixed)
    try require(try labels() == ["\(photoFace.key)#\(son.id)#confirmed", "\(videoFace.key)#\(son.id)#confirmed", "\(otherFace.key)#\(son.id)#rejected"],
                "labels after confirm/reject")
    let moved = try catalog.apply(.confirm([videoFace], as: daughter.id), now: fixed)
    try require(try labels() == ["\(photoFace.key)#\(son.id)#confirmed", "\(videoFace.key)#\(daughter.id)#confirmed", "\(otherFace.key)#\(son.id)#rejected"],
                "confirming another person must replace the face's previous confirmation")
    try catalog.restore(moved, now: fixed)
    try require(try labels().contains("\(videoFace.key)#\(son.id)#confirmed") && !(try labels()).contains("\(videoFace.key)#\(daughter.id)#confirmed"),
                "undo of a move restores the original person")
    try require(moved.labels.count == 1, "confirm undo scope must contain only the moved face's labels")
    let merged = try catalog.apply(.merge(daughter.id, into: son.id), now: fixed)
    try require(try catalog.people().map(\.id) == [son.id], "merge removes the merged person")
    try catalog.restore(merged, now: fixed)
    try require(Set(try catalog.people().map(\.id)) == [son.id, daughter.id], "merge undo restores both people")
    try catalog.restore(confirmed, now: fixed)
    try require(try labels() == ["\(otherFace.key)#\(son.id)#rejected"], "undo of confirmation restores exact prior labels")
    try catalog.apply(.delete(son.id), now: fixed)
    try require(try labels().isEmpty && (try catalog.people().map(\.id)) == [daughter.id], "deleting a person removes only their labels")
    try catalog.restore(created, now: fixed)
    try require(try catalog.people().map(\.id) == [daughter.id], "creation undo scope is only the created person")
    let history = try catalog.userAccess { Int(try $0.scalar("SELECT count(*) FROM people_history")) ?? 0 }
    try require(history == 11, "every edit and undo is recorded in history (found \(history))")
    try require(FaceRef(asset: "a", timestamp: 1.0000004, box: [0.1234564, 0, 1, 1]).key == FaceRef(asset: "a", timestamp: 1, box: [0.123456, 0, 1, 1]).key,
                "face keys are stable across float noise")
    print("PEOPLE store edits, one confirmation per face, merge/delete scope, exact undo and history passed")
}
