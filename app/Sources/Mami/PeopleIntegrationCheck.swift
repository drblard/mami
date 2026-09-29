import AppKit
import SwiftUI

/// End-to-end people workflow on an isolated catalog whose face index was built
/// by face_worker.py: name a group, let the real worker recompute, filter, undo.
@MainActor func checkPeopleWorkflow(at directory: URL) async throws {
    guard ProcessInfo.processInfo.environment["MAMI_CATALOG"] != nil else {
        throw AppError.message("People checks require an isolated MAMI_CATALOG")
    }
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
    func require(_ condition: @autoclosure () throws -> Bool, _ message: String) throws {
        if try !condition() { throw AppError.message("PEOPLE WORKFLOW: " + message) }
    }
    let catalog = Catalog.standard
    let model = PeopleLibrary(catalog: catalog)
    await model.reload()
    try require(model.error == nil, model.error ?? "")
    let group = try model.groups.first.unwrap("face index has no groups to review")
    model.show(.group(group.id))
    let deadline = ContinuousClock.now.advanced(by: .seconds(10))
    while model.faces.isEmpty && ContinuousClock.now < deadline { try await Task.sleep(for: .milliseconds(50)) }
    try require(model.faces.count == min(group.count, FaceIndex.faceLimit), "expected \(group.count) group faces, loaded \(model.faces.count)")
    try require(model.selected == Set(model.faces.map(\.id)), "a group should start fully selected")
    // Leave one face out, as the user would for a wrong match.
    let named = model.faces.count > 1 ? Set(model.faces.dropLast().map(\.id)) : Set(model.faces.map(\.id))
    let expectedAssets = Set(model.faces.filter { named.contains($0.id) }.map(\.ref.asset))
    let generation = Indexing.faces.catalogGeneration
    model.selected = named
    model.assign(named, toNew: "Check person")
    let person = try (try catalog.people()).first { $0.name == "Check person" }.unwrap("named person was not saved")
    let saved = try catalog.userAccess { db in
        Int(try db.scalar("SELECT count(*) FROM face_labels WHERE person=? AND verdict='confirmed'", [person.id])) ?? -1
    }
    try require(saved == named.count, "expected \(named.count) confirmations, saved \(saved)")
    // The real worker regroups and suggests; wait for its change notification.
    let recomputed = ContinuousClock.now.advanced(by: .seconds(120))
    while Indexing.faces.catalogGeneration == generation {
        try require(ContinuousClock.now < recomputed, "face worker did not recompute: \(Indexing.faces.phase) \(Indexing.faces.error ?? "")")
        try await Task.sleep(for: .milliseconds(100))
    }
    await model.reload()
    let assets = try FaceIndex.assets(withAll: [person.id], catalog)
    try require(expectedAssets.isSubset(of: assets), "filter lost confirmed media")
    try require((model.counts[person.id]?.media ?? 0) == assets.count, "person media count disagrees with the filter")
    try require(!model.groups.contains { $0.id == group.id && $0.count == group.count }, "named group is still offered unchanged")
    model.show(.person(person.id))
    try await Task.sleep(for: .milliseconds(500))
    let host = NSHostingView(rootView: PeopleView(model: model, close: {}).preferredColorScheme(.dark))
    host.appearance = NSAppearance(named: .darkAqua)
    let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1100, height: 720), styleMask: [.titled], backing: .buffered, defer: false)
    window.contentView = host
    window.orderFrontRegardless()
    try await Task.sleep(for: .seconds(1))
    host.layoutSubtreeIfNeeded()
    if let bitmap = host.bitmapImageRepForCachingDisplay(in: host.bounds) {
        host.cacheDisplay(in: host.bounds, to: bitmap)
        try bitmap.representation(using: .png, properties: [:])?.write(to: directory.appendingPathComponent("people.png"), options: .withoutOverwriting)
    }
    window.orderOut(nil)
    model.undo()
    let remaining = try catalog.userAccess { db in Int(try db.scalar("SELECT count(*) FROM face_labels")) ?? -1 }
    try require(remaining == 0 && (try catalog.people()).isEmpty, "undo did not remove the person and labels")
    Indexing.faces.stop()
    let report: [String: Any] = ["status": "passed", "group_faces": group.count, "named": named.count,
                               "person_media": assets.count, "confirmed_assets": expectedAssets.count]
    let data = try JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys])
    try data.write(to: directory.appendingPathComponent("result.json"), options: .withoutOverwriting)
    print("PEOPLE WORKFLOW PASSED: \(String(decoding: data, as: UTF8.self))")
}

extension Optional {
    func unwrap(_ message: String) throws -> Wrapped {
        guard let value = self else { throw AppError.message(message) }
        return value
    }
}
