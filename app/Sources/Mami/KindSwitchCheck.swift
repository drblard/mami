import AppKit
import SwiftUI

/// Drives the real kind picker in a hosted LibraryView and captures the grid
/// after each switch, reproducing thumbnails lost when changing Videos/Photos.
@MainActor func checkKindSwitching(at directory: URL) async throws {
    guard ProcessInfo.processInfo.environment["MAMI_CATALOG"] != nil else {
        throw AppError.message("Kind-switch checks require an isolated MAMI_CATALOG")
    }
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
    let library = Library()
    await library.load()
    guard library.ready, library.usesProjection else { throw AppError.message(library.error ?? "Paged library did not become ready") }
    let host = NSHostingView(rootView: LibraryView(library: library, clips: ClipSelection(catalog: .standard), navigation: BrowserSelection()))
    host.appearance = NSAppearance(named: .darkAqua)
    let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1200, height: 800), styleMask: [.titled], backing: .buffered, defer: false)
    window.contentView = host
    window.orderFrontRegardless()
    func segmented(in view: NSView) -> NSSegmentedControl? {
        if let control = view as? NSSegmentedControl, control.segmentCount == 3, control.label(forSegment: 1) == "Videos" { return control }
        return view.subviews.lazy.compactMap { segmented(in: $0) }.first
    }
    func snapshot(_ name: String) async throws {
        host.layoutSubtreeIfNeeded()
        guard let bitmap = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { throw AppError.message("Cannot capture grid") }
        host.cacheDisplay(in: host.bounds, to: bitmap)
        try bitmap.representation(using: .png, properties: [:])?.write(to: directory.appendingPathComponent(name + ".png"), options: .withoutOverwriting)
        let kinds = Dictionary(grouping: library.items, by: \.kind).mapValues(\.count)
        print("KIND SWITCH \(name) items=\(kinds) cached=\(await FrameCache.shared.retainedCount)")
    }
    try await Task.sleep(for: .seconds(3))
    try await snapshot("0-all")
    guard let picker = segmented(in: host) else { throw AppError.message("Kind picker control not found") }
    for (step, segment) in [(1, 1), (2, 2), (3, 1)] {
        picker.selectedSegment = segment
        _ = picker.sendAction(picker.action, to: picker.target)
        let name = "\(step)-\(picker.label(forSegment: segment) ?? String(segment))"
        try await Task.sleep(for: .milliseconds(1500))
        try await snapshot(name + "-early")
        try await Task.sleep(for: .seconds(6))
        try await snapshot(name + "-late")
    }
    window.orderOut(nil)
    await library.worker.stop()
    print("KIND SWITCH CHECK COMPLETED: \(directory.path)")
}
