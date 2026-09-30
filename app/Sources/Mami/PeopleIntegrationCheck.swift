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
    try await checkNameFieldAutofocus()
    let catalog = Catalog.standard
    try require(try catalog.people().isEmpty, "fixture must start without people; rebuild it after a failed run")
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
    let host = NSHostingView(rootView: PeopleView(model: model, close: {}))
    host.appearance = NSAppearance(named: .darkAqua)
    let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1100, height: 720), styleMask: [.titled], backing: .buffered, defer: false)
    window.contentView = host
    window.makeKeyAndOrderFront(nil)
    NSApp.activate(ignoringOtherApps: true)
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

/// Names several groups in succession, spaced to land in the worker's idle
/// retirement window, and clicks other groups in between (reported by the user:
/// a named group stayed listed and every group then showed 0 faces).
@MainActor func checkPeopleSequence(at directory: URL) async throws {
    guard ProcessInfo.processInfo.environment["MAMI_CATALOG"] != nil else {
        throw AppError.message("People checks require an isolated MAMI_CATALOG")
    }
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
    func require(_ condition: @autoclosure () throws -> Bool, _ message: String) throws {
        if try !condition() { throw AppError.message("PEOPLE SEQUENCE: " + message) }
    }
    func wait(_ description: String, seconds: Double, _ predicate: () throws -> Bool) async throws {
        let deadline = ContinuousClock.now.advanced(by: .milliseconds(Int(seconds * 1000)))
        while !(try predicate()) {
            try require(ContinuousClock.now < deadline, "timed out: \(description); faces lane \(Indexing.faces.phase) running=\(Indexing.faces.running) error=\(Indexing.faces.error ?? "none")")
            try await Task.sleep(for: .milliseconds(50))
        }
    }
    let catalog = Catalog.standard
    try require(try catalog.people().isEmpty, "fixture must start without people")
    let model = PeopleLibrary(catalog: catalog)
    // Host the real views: their own change handlers drive reloads, as in the app.
    let host = NSHostingView(rootView: VStack {
        PeopleFilterPicker(model: model, selected: .constant([]), manage: {})
        PeopleView(model: model, close: {})
    })
    let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1100, height: 760), styleMask: [.titled], backing: .buffered, defer: false)
    window.contentView = host
    window.orderFrontRegardless()
    defer { window.orderOut(nil) }
    await model.reload()
    var named = 0
    for (step, pause) in [16.0, 18.0, 20.0].enumerated() {
        guard model.groups.count >= 2 else { break }
        let group = model.groups[0], next = model.groups[1]
        model.show(.group(group.id))
        try await wait("group \(step) loads", seconds: 5) { model.faces.count == min(group.count, FaceIndex.faceLimit) }
        model.assign(Set(model.faces.map(\.id)), toNew: "Sequence \(step)")
        named += 1
        model.show(.group(next.id))
        try await wait("next group \(step) shows its faces", seconds: 5) { model.faces.count == min(next.count, FaceIndex.faceLimit) }
        try await wait("named group \(step) leaves the list", seconds: 60) {
            model.focus == .group(next.id) && !model.groups.contains(where: { $0.id == group.id })
        }
        print("PEOPLE SEQUENCE step \(step): named \(group.count) faces; next group loaded; list updated"); fflush(stdout)
        try await Task.sleep(for: .milliseconds(Int(pause * 1000)))
    }
    try require(named >= 2, "fixture has too few groups for a sequence")
    try await wait("worker is released after the sequence", seconds: 45) { !Indexing.faces.running }
    try require(Indexing.faces.error == nil, "faces lane error: \(Indexing.faces.error ?? "")")
    while !model.edits.isEmpty { model.undo() }
    let remaining = try catalog.userAccess { db in Int(try db.scalar("SELECT count(*) FROM face_labels")) ?? -1 }
    try require(remaining == 0 && (try catalog.people()).isEmpty, "undo left labels behind")
    Indexing.faces.stop()
    print("PEOPLE SEQUENCE PASSED: \(named) groups named in succession")
}

/// The name field in a real popover must accept typing without a click.
@MainActor func checkNameFieldAutofocus() async throws {
    let anchorWindow = NSWindow(contentRect: NSRect(x: 100, y: 100, width: 400, height: 200), styleMask: [.titled], backing: .buffered, defer: false)
    let anchor = NSView(frame: NSRect(x: 150, y: 80, width: 100, height: 30))
    anchorWindow.contentView = NSView(frame: NSRect(x: 0, y: 0, width: 400, height: 200))
    anchorWindow.contentView?.addSubview(anchor)
    anchorWindow.makeKeyAndOrderFront(nil)
    NSApp.activate(ignoringOtherApps: true)
    var typed = ""
    let popover = NSPopover()
    popover.contentViewController = NSHostingController(rootView: AutofocusTextField(placeholder: "New person’s name",
        text: Binding(get: { typed }, set: { typed = $0 })).frame(width: 240).padding(16))
    popover.show(relativeTo: anchor.bounds, of: anchor, preferredEdge: .maxY)
    defer { popover.close(); anchorWindow.orderOut(nil) }
    let deadline = ContinuousClock.now.advanced(by: .seconds(3))
    func focusedField() -> AutofocusTextField.Field? {
        guard let editor = popover.contentViewController?.view.window?.firstResponder as? NSTextView else { return nil }
        return editor.delegate as? AutofocusTextField.Field
    }
    while focusedField() == nil {
        guard ContinuousClock.now < deadline else {
            throw AppError.message("Name field was not focused in its popover; first responder \(String(describing: popover.contentViewController?.view.window?.firstResponder))")
        }
        try await Task.sleep(for: .milliseconds(50))
    }
    // Typing goes into the focused field and reaches the binding.
    focusedField()?.currentEditor()?.insertText("Ana")
    try await Task.sleep(for: .milliseconds(100))
    guard typed == "Ana" else { throw AppError.message("Typing into the focused name field did not update the name (\(typed))") }
    print("PEOPLE name field is focused in a real popover and typing updates the name")
}
