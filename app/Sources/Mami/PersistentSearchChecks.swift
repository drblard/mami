import Foundation
import AppKit
import SwiftUI

@MainActor func checkPersistentSearch(at directory: URL) async throws {
    guard ProcessInfo.processInfo.environment["MAMI_CATALOG"] != nil else {
        throw AppError.message("Persistent search checks require an isolated MAMI_CATALOG")
    }
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
    try checkProjection(at: directory)
    try await checkPreviewCache(at: directory)
    let library = Library()
    let clock = ContinuousClock()
    let started = clock.now
    await library.load()
    guard library.ready else { throw AppError.message(library.error ?? "Library did not become ready") }
    let readyDuration = started.duration(to: clock.now).components
    let readySeconds = Double(readyDuration.seconds) + Double(readyDuration.attoseconds)/1e18
    let loadedCount = library.items.count
    var scrubChecks: [String: Any] = [:]
    if let projection = library.projectionReader,
       let video = try projection.page(scope: .init(kind: "video"), limit: 1).items.first {
        let frames = try projection.frames(asset: video.assetID)
        guard !frames.isEmpty else { throw AppError.message("Video has no cached scrub frames") }
        let ordinals = Set([0, frames.count/2, frames.count-1])
        for ordinal in ordinals {
            let sample = frames[ordinal]
            guard let image = await FrameCache.shared.image(sample, assetID: video.assetID, projection: projection) else {
                throw AppError.message("Cached scrub frame could not be decoded")
            }
            if let crop = sample.crop {
                guard image.size.width == CGFloat(crop[2]), image.size.height == CGFloat(crop[3]) else {
                    throw AppError.message("Packed scrub frame has incorrect dimensions")
                }
            }
        }
        scrubChecks = ["decoded_samples": ordinals.count, "cached_frames": frames.count,
                       "original_available": FileManager.default.isReadableFile(atPath: video.url.path),
                       "packed": frames.contains { $0.crop != nil }]
    }
    guard loadedCount <= ProjectionReader.pageSize, library.totalMediaCount >= loadedCount else {
        throw AppError.message("Startup loaded more than the first page")
    }
    let initialIDs = Set(library.items.map(\.assetID))
    if library.canLoadMore {
        await library.loadMore()
        guard library.items.count > loadedCount, Set(library.items.map(\.assetID)).count == library.items.count,
              initialIDs.isSubset(of: Set(library.items.map(\.assetID))) else { throw AppError.message("Native next-page loading lost or duplicated media") }
    }
    if let camera = library.devices.first {
        library.deviceFilter = camera
        await library.search()?.value
        guard library.items.allSatisfy({ $0.device == camera }) else { throw AppError.message("Paged camera filter escaped its scope") }
        library.deviceFilter = "All devices"
    }
    var results: [[String: Any]] = []
    library.query = "a photo"
    await library.search()?.value
    guard library.error == nil, !library.searching else { throw AppError.message(library.error ?? "Visual warmup failed") }
    let visualWarmup = started.duration(to: clock.now).components
    let visualReadyBySeconds = Double(visualWarmup.seconds)+Double(visualWarmup.attoseconds)/1e18
    for query in ["milking goats", "bringing food to goats", "dunare"] {
        let before = clock.now
        library.query = query
        await library.search()?.value
        guard library.error == nil, library.showingMatches, !library.items.isEmpty else {
            await library.worker.stop()
            throw AppError.message(library.error ?? "Search returned no visible media")
        }
        let elapsed = before.duration(to: clock.now).components
        results.append(["query": query, "seconds": Double(elapsed.seconds)+Double(elapsed.attoseconds)/1e18,
                        "hits": library.items.count, "status": library.status])
    }
    library.query = ""
    await library.search()?.value
    let clips = ClipSelection(catalog: .standard)
    let navigation = BrowserSelection()
    let host = NSHostingView(rootView: LibraryView(library: library, clips: clips, navigation: navigation))
    host.appearance = NSAppearance(named: .darkAqua)
    let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1200, height: 800), styleMask: [.titled], backing: .buffered, defer: false)
    window.contentView = host
    window.orderFrontRegardless()
    try await Task.sleep(for: .milliseconds(750))
    host.layoutSubtreeIfNeeded()
    guard let bitmap = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { throw AppError.message("Cannot capture paged grid") }
    host.cacheDisplay(in: host.bounds, to: bitmap)
    try bitmap.representation(using: .png, properties: [:])?.write(to: directory.appendingPathComponent("paged-grid.png"), options: .withoutOverwriting)
    window.orderOut(nil)
    await library.worker.stop()
    let report: [String: Any] = ["ready_seconds": readySeconds, "visual_ready_by_seconds": visualReadyBySeconds,
                               "loaded_items": loadedCount, "queries": results, "scrub_checks": scrubChecks,
                               "scope": "Native Library load and search, isolated catalog, warm OS cache possible; not a reboot measurement."]
    let data = try JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys])
    try data.write(to: directory.appendingPathComponent("result.json"), options: .withoutOverwriting)
    print(String(decoding: data, as: UTF8.self))
}
