import SwiftUI
import AVKit
import ImageIO
import MamiCore

// Explicit alias selects the property wrapper on SDKs that also export a State macro.
typealias ViewState<Value> = SwiftUI.State<Value>

actor FrameCache {
    static let shared = FrameCache()
    private enum Limits {
        static let imageBytes = 96 * 1024 * 1024
        static let imageCount = 256
        static let atlasBytes = 32 * 1024 * 1024
        static let atlasCount = 4
        static let maximumAtlasDimension = 4096
    }
    /// NSCache synchronizes access internally. Values are immutable CGImages,
    /// and configuration is set before this wrapper crosses an executor boundary.
    private final class AtlasCache: @unchecked Sendable {
        private let storage = NSCache<NSString, CGImage>()
        init() {
            storage.totalCostLimit = Limits.atlasBytes
            storage.countLimit = Limits.atlasCount
        }
        func image(for path: String) -> CGImage? { storage.object(forKey: path as NSString) }
        func insert(_ image: CGImage, for path: String, cost: Int) {
            storage.setObject(image, forKey: path as NSString, cost: cost)
        }
    }
    private var images: [String: (NSImage, Int)] = [:]
    private var order: [String] = []
    private var bytes = 0
    private var pending: [String: Task<NSImage?, Never>] = [:]
    private let queue = DispatchQueue(label: "mami.frames", qos: .userInitiated)
    private let atlases = AtlasCache()
    var retainedCost: Int { bytes }
    var retainedCount: Int { images.count }

    func image(_ sample: Sample, assetID: String, projection: ProjectionReader?, maxPixelSize: Int = 640) async -> NSImage? {
        if let cached = await image(sample.frame, maxPixelSize: maxPixelSize, crop: sample.crop) { return cached }
        let timestamp = sample.timestamp
        let replacement = try? await Task.detached(priority: .userInitiated) {
            if let projection { return try projection.nearestFrame(asset: assetID, timestamp: timestamp) }
            return try Catalog.standard.media(forAssetIDs: [assetID]).first?.frames.min {
                abs(($0.timestamp ?? 0)-(timestamp ?? 0)) < abs(($1.timestamp ?? 0)-(timestamp ?? 0))
            }
        }.value
        guard let replacement, replacement.cacheKey != sample.cacheKey else { return nil }
        return await image(replacement.frame, maxPixelSize: maxPixelSize, crop: replacement.crop)
    }

    func image(_ path: String, maxPixelSize: Int = 640, crop: [Int]? = nil) async -> NSImage? {
        guard !path.isEmpty else { return nil }
        let key = "\(maxPixelSize):\(path):\(crop ?? [])"
        if let cached = images[key] {
            order.removeAll { $0 == key }; order.append(key)
            return cached.0
        }
        if let task = pending[key] { return await task.value }
        guard !Task.isCancelled else { return nil }
        let task = Task<NSImage?, Never> { [queue, atlases] in
        await withCheckedContinuation { continuation in
            queue.async {
                autoreleasepool {
                if let crop {
                    guard crop.count == 4, crop[0] >= 0, crop[1] >= 0, crop[2] > 0, crop[3] > 0 else { continuation.resume(returning: nil); return }
                    let atlas: CGImage
                    if let cached = atlases.image(for: path) { atlas = cached }
                    else {
                        guard let source = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil),
                              let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
                              let width = properties[kCGImagePropertyPixelWidth] as? NSNumber,
                              let height = properties[kCGImagePropertyPixelHeight] as? NSNumber,
                              width.intValue > 0, height.intValue > 0,
                              width.intValue <= Limits.maximumAtlasDimension, height.intValue <= Limits.maximumAtlasDimension,
                              let decoded = CGImageSourceCreateImageAtIndex(source, 0, [kCGImageSourceShouldCacheImmediately: true] as CFDictionary) else { continuation.resume(returning: nil); return }
                        atlas = decoded
                        let cost = decoded.bytesPerRow * decoded.height
                        if cost <= Limits.atlasBytes { atlases.insert(decoded, for: path, cost: cost) }
                    }
                    guard crop[0] <= atlas.width - crop[2], crop[1] <= atlas.height - crop[3],
                          let cg = atlas.cropping(to: CGRect(x: crop[0], y: crop[1], width: crop[2], height: crop[3])) else { continuation.resume(returning: nil); return }
                    // Detach the crop's pixels: CGImage crops may otherwise keep
                    // an entire sheet alive outside the atlas cache's budget.
                    guard let space = CGColorSpace(name: CGColorSpace.sRGB),
                          let context = CGContext(data: nil, width: cg.width, height: cg.height, bitsPerComponent: 8,
                                                  bytesPerRow: cg.width * 4, space: space,
                                                  bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { continuation.resume(returning: nil); return }
                    context.draw(cg, in: CGRect(x: 0, y: 0, width: cg.width, height: cg.height))
                    guard let detached = context.makeImage() else { continuation.resume(returning: nil); return }
                    continuation.resume(returning: NSImage(cgImage: detached, size: NSSize(width: detached.width, height: detached.height)))
                    return
                }
                guard let source = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil),
                      let cg = CGImageSourceCreateThumbnailAtIndex(source, 0, [
                        kCGImageSourceCreateThumbnailFromImageAlways: true,
                        kCGImageSourceThumbnailMaxPixelSize: maxPixelSize,
                        kCGImageSourceCreateThumbnailWithTransform: true,
                        kCGImageSourceShouldCacheImmediately: true
                      ] as CFDictionary) else { continuation.resume(returning: nil); return }
                let image = NSImage(cgImage: cg, size: NSSize(width: cg.width, height: cg.height))
                continuation.resume(returning: image)
                }
            }
        }
        }
        pending[key] = task
        let image = await task.value
        pending[key] = nil
        if let image {
            let cost = Int(image.size.width) * Int(image.size.height) * 4
            while !order.isEmpty && (bytes + cost > Limits.imageBytes || order.count >= Limits.imageCount) {
                let oldest = order.removeFirst()
                if let removed = images.removeValue(forKey: oldest) { bytes -= removed.1 }
            }
            if cost <= Limits.imageBytes { images[key] = (image, cost); order.append(key); bytes += cost }
        }
        return image
    }
}

func timeLabel(_ seconds: Double?) -> String {
    guard let seconds else { return "Photo" }
    guard seconds.isFinite else { return "--:--" }
    let value = max(0, Int(seconds))
    if value >= 3600 { return String(format: "%d:%02d:%02d", value / 3600, value / 60 % 60, value % 60) }
    return String(format: "%02d:%02d", value / 60, value % 60)
}

struct MediaCard: View {
    let media: Media
    var projection: ProjectionReader? = nil
    let nearby: Bool
    @ObservedObject var annotations: Annotations
    @ObservedObject var clips: ClipSelection
    var focused = false
    var select: () -> Void = {}
    var dragItems: () -> [Media] = { [] }
    var dragEnabled = true
    let open: (Media, Double?) -> Void
    @ViewState private var hovered: Int?
    @ViewState private var hoverFraction: CGFloat?
    @ViewState private var image: NSImage?
    @ViewState private var unavailable = false
    @ViewState private var pointerInside = false
    @ViewState private var loadedFrames: [Sample]?
    private var annotation: Annotation { annotations.value(for: media) }
    private var subtitle: String {
        let labels = ([annotation.place].filter { !$0.isEmpty } + annotation.tags.map { "#" + $0 })
        if !labels.isEmpty { return labels.joined(separator: " · ") }
        if media.previewState == "pending" { return media.kind == "video" ? "Playable · Preview queued" : "Imported · Preview queued" }
        if media.previewState == "partial" { return "Playable · Building scrub previews" }
        return media.match.evidence.map { "“\($0)”" } ?? media.metadata?.subtitle ?? media.kind.capitalized
    }
    private var scrubFrames: [Sample] {
        let frames = loadedFrames ?? media.frames
        guard nearby, let timestamp = media.match.timestamp else { return frames }
        return frames.filter { abs(($0.timestamp ?? 0) - timestamp) <= 8 }
    }
    private var sample: Sample {
        if let hovered, scrubFrames.indices.contains(hovered) { return scrubFrames[hovered] }
        return media.match
    }

    private func scrubIndex(at fraction: CGFloat) -> Int? {
        guard media.kind == "video", !scrubFrames.isEmpty else { return nil }
        let times = scrubFrames.map { $0.timestamp ?? 0 }
        let lower = nearby ? (times.first ?? 0) : 0
        let upper = nearby ? (times.last ?? lower) : (media.metadata?.duration ?? times.last ?? lower)
        let target = lower + Double(min(1, max(0, fraction))) * max(0, upper-lower)
        return ScrubTimeline.nearestIndex(in: times, to: target)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            GeometryReader { geometry in
                ZStack(alignment: .bottomLeading) {
                    Rectangle().fill(Color.black.opacity(0.65))
                    if let image { Image(nsImage: image).resizable().scaledToFit().frame(maxWidth: .infinity, maxHeight: .infinity) }
                    else { Image(systemName: unavailable ? "exclamationmark.triangle" : "photo").frame(maxWidth: .infinity, maxHeight: .infinity).foregroundStyle(.secondary) }
                    Text(timeLabel(sample.timestamp)).font(.caption.monospacedDigit()).padding(5).background(.black.opacity(0.7)).padding(5)
                    if hovered != nil, let fraction = hoverFraction {
                        Rectangle().fill(Color.accentColor).frame(width: geometry.size.width * min(1, max(0, fraction)), height: 3)
                    }
                }
                .overlay(alignment: .topTrailing) {
                    Button { annotations.toggleFavorite(media) } label: {
                        Image(systemName: annotation.favorite ? "heart.fill" : "heart")
                            .foregroundStyle(annotation.favorite ? Color.pink : Color.white)
                            .padding(7).background(.black.opacity(0.6), in: Circle())
                    }.buttonStyle(.plain).padding(7).disabled(!annotations.ready)
                        .help(annotation.favorite ? "Remove from favorites" : "Add to favorites")
                }
                .overlay(alignment: .topLeading) {
                    Button { clips.toggle(media, sample: sample) } label: {
                        Image(systemName: clips.contains(media) ? "checkmark.circle.fill" : "plus.circle.fill")
                            .foregroundStyle(clips.contains(media) ? Color.accentColor : Color.white)
                            .padding(7).background(.black.opacity(0.6), in: Circle())
                    }.buttonStyle(.plain).padding(7).disabled(!clips.ready)
                        .help(clips.contains(media) ? "Remove from selected clips" : "Add to selected clips")
                        .accessibilityLabel(clips.contains(media) ? "Remove from selected clips" : "Add to selected clips")
                }
                .contentShape(Rectangle())
                .onContinuousHover { phase in
                    switch phase {
                    case .active(let point):
                        guard media.kind == "video" else { return }
                        let fraction = point.x / max(1, geometry.size.width)
                        hoverFraction = fraction
                        hovered = scrubIndex(at: fraction)
                    case .ended: hovered = nil; hoverFraction = nil
                    }
                }
                .onTapGesture(count: 2) { open(media, sample.timestamp) }
                .onTapGesture { select() }
            }.frame(height: 175).clipShape(RoundedRectangle(cornerRadius: 8))
            HStack(spacing: 6) {
                Text(media.metadata?.date ?? "Capture date unavailable").font(.system(size: 12, weight: .medium)).lineLimit(1)
                Spacer(minLength: 0)
                Text(media.kind == "image" ? "Photo" : media.metadata?.duration.map { timeLabel($0) } ?? "Video")
                    .font(.system(size: 14, weight: .semibold).monospacedDigit()).foregroundStyle(.primary)
            }
            Text(subtitle)
                .font(.caption).foregroundStyle(.secondary).lineLimit(1)
                .help(([media.title, media.metadata?.subtitle ?? "", media.match.evidence ?? ""] + (media.metadata?.tags ?? []) + (media.metadata?.technical ?? [])).filter { !$0.isEmpty }.joined(separator: "\n"))
        }
        .padding(9)
        .background(Color(red: 0.14, green: 0.15, blue: 0.17), in: RoundedRectangle(cornerRadius: 10))
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(focused ? Color.accentColor : pointerInside ? Color.accentColor.opacity(0.6) : .clear, lineWidth: 2))
        .contentShape(RoundedRectangle(cornerRadius: 10))
        .onHover { pointerInside = $0 }
        .task(id: "\(pointerInside):\(media.frameCount ?? media.frames.count):\(media.match.cacheKey)") {
            guard pointerInside, media.kind == "video", let projection else { return }
            let asset = media.assetID
            do {
                let frames = try await Task.detached(priority: .userInitiated) { try projection.frames(asset: asset) }.value
                guard !Task.isCancelled else { return }
                loadedFrames = frames
                if let fraction = hoverFraction { hovered = scrubIndex(at: fraction) }
            } catch { unavailable = true }
        }
        .background(MediaDragSurface(enabled: dragEnabled, items: dragItems))
        .onTapGesture { select() }
        .task(id: sample.cacheKey) {
            if hovered != nil { try? await Task.sleep(for: .milliseconds(35)) }
            guard !Task.isCancelled else { return }
            let path = sample.frame
            let decoded = await FrameCache.shared.image(sample, assetID: media.assetID, projection: projection)
            guard !Task.isCancelled else { return }
            image = decoded
            unavailable = decoded == nil && !path.isEmpty
            if let position = scrubFrames.firstIndex(where: { $0.frame == path }) {
                for neighbor in [position + 1, position - 1, position + 2, position - 2] {
                    guard !Task.isCancelled else { return }
                    if scrubFrames.indices.contains(neighbor) {
                        _ = await FrameCache.shared.image(scrubFrames[neighbor].frame, crop: scrubFrames[neighbor].crop)
                    }
                }
            }
        }
        .onDisappear { image = nil; loadedFrames = nil; hovered = nil; pointerInside = false }
        .onChange(of: media.match.cacheKey) { _, _ in hovered = nil }
        .onChange(of: media.frames.count) { _, _ in
            if let fraction = hoverFraction { hovered = scrubIndex(at: fraction) }
        }
        .onChange(of: nearby) { _, _ in
            if let fraction = hoverFraction { hovered = scrubIndex(at: fraction) }
            else { hovered = nil }
        }
        .contextMenu {
            Button("Play from match") { open(media, media.match.timestamp) }
            Button("Reveal in Finder") { NSWorkspace.shared.activateFileViewerSelecting([media.url]) }
        }
        .help("Click to select · ⌘ click to add/remove · Drag originals · Space or double-click to preview")
    }
}

struct Selection: Identifiable {
    let id = UUID()
    let media: Media
    let timestamp: Double?
}

@MainActor func preparedPlayer(url: URL, timestamp: Double) async throws -> AVPlayer {
    let asset = AVURLAsset(url: url)
    guard try await asset.load(.isPlayable) else { throw AppError.message("This video is not playable.") }
    let duration = try await asset.load(.duration).seconds
    let item = AVPlayerItem(asset: asset)
    let player = AVPlayer(playerItem: item)
    player.automaticallyWaitsToMinimizeStalling = false
    // Seeking immediately after constructing AVPlayer may report success before
    // the item is ready, leaving the requested starting position unapplied.
    let deadline = ContinuousClock.now.advanced(by: .seconds(10))
    while item.status == .unknown && ContinuousClock.now < deadline {
        try await Task.sleep(for: .milliseconds(20))
    }
    guard item.status == .readyToPlay else {
        throw AppError.message(item.error?.localizedDescription ?? "Video did not become ready within ten seconds.")
    }
    let target = max(0, min(timestamp, duration.isFinite ? max(0, duration - 0.05) : timestamp))
    let ok = await player.seek(to: CMTime(seconds: target, preferredTimescale: 600), toleranceBefore: .zero, toleranceAfter: CMTime(seconds: 0.1, preferredTimescale: 600))
    guard ok else { throw AppError.message("Could not seek this video.") }
    return player
}

struct Playback: View {
    let selection: Selection
    @ObservedObject var annotations: Annotations
    @ObservedObject var clips: ClipSelection
    var close: () -> Void = {}
    var position: String = ""
    @StateObject private var transport = PlaybackTransport()
    @ViewState private var photo: NSImage?
    @ViewState private var failure: String?
    @ViewState private var showInfo = false
    @ViewState private var editingMetadata = false
    @ViewState private var tags = ""
    @ViewState private var place = ""
    @ViewState private var pixels = CGSize.zero
    @ViewState private var actualSize = false
    @ViewState private var zoom: CGFloat = 1
    @Environment(\.displayScale) private var displayScale

    var body: some View {
        VStack(spacing: 12) {
            HStack {
                VStack(alignment: .leading) {
                    Text("\(selection.media.metadata?.date ?? selection.media.title) · \(Int((zoom * 100).rounded()))%").font(.headline)
                    Text(selection.media.metadata?.subtitle ?? selection.media.path).font(.caption).foregroundStyle(.secondary).help(selection.media.path)
                }
                Spacer()
                if !position.isEmpty { Text(position).monospacedDigit().foregroundStyle(.secondary).accessibilityLabel("Preview \(position)") }
                Button { annotations.toggleFavorite(selection.media) } label: {
                    Image(systemName: annotations.value(for: selection.media).favorite ? "heart.fill" : "heart")
                }.disabled(!annotations.ready).help("Toggle favorite")
                Button("Tags & place") {
                    let value = annotations.value(for: selection.media)
                    tags = value.tags.joined(separator: ", ")
                    place = value.place
                    editingMetadata = true
                }.disabled(!annotations.ready)
                    .popover(isPresented: $editingMetadata) {
                        VStack(alignment: .leading, spacing: 12) {
                            Text("Organize this moment").font(.headline)
                            TextField("Tags, separated by commas", text: $tags)
                            TextField("Place — e.g. grandparents’ farm", text: $place)
                            Text("Your labels are saved locally. Camera metadata stays separate.").font(.caption).foregroundStyle(.secondary)
                            HStack {
                                Button("Cancel") { editingMetadata = false }
                                Spacer()
                                Button("Save") {
                                    var value = annotations.value(for: selection.media)
                                    var seen = Set<String>()
                                    value.tags = tags.split(separator: ",").map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
                                        .filter { !$0.isEmpty && seen.insert($0.lowercased()).inserted }
                                    value.place = place.trimmingCharacters(in: .whitespacesAndNewlines)
                                    annotations.save(value, for: selection.media)
                                    if annotations.error == nil { editingMetadata = false }
                                }.buttonStyle(.borderedProminent)
                            }
                            if let error = annotations.error { Text(error).foregroundStyle(.red).font(.caption) }
                        }.padding(20).frame(width: 340)
                    }
                Button(actualSize ? "Fit" : "100%") { actualSize.toggle() }.help("Fit to available space or show original pixels")
                Button { showInfo.toggle() } label: { Image(systemName: "info.circle") }
                    .help("File and capture information")
                    .popover(isPresented: $showInfo) {
                        VStack(alignment: .leading, spacing: 10) {
                            Text("Capture information").font(.headline)
                            Text(selection.media.metadata?.date ?? "Capture date unavailable")
                            Text(selection.media.metadata?.subtitle ?? selection.media.kind.capitalized)
                            if let metadata = selection.media.metadata {
                                Text(metadata.location ?? "No GPS location recorded").foregroundStyle(.secondary)
                                ForEach(metadata.technical, id: \.self) { Text($0) }
                                if !metadata.tags.isEmpty {
                                    Text("Camera keywords: " + metadata.tags.joined(separator: ", "))
                                }
                            }
                            Divider()
                            Text(selection.media.url.path).font(.caption).textSelection(.enabled)
                            if let evidence = selection.media.match.evidence { Text(evidence).font(.callout).textSelection(.enabled) }
                        }.padding(20).frame(width: 340, alignment: .leading)
                    }
                Button(clips.contains(selection.media) ? "Remove from selection" : "Add to selection") { clips.toggle(selection.media) }
                    .disabled(!clips.ready).keyboardShortcut("s", modifiers: [.command, .shift])
                Button("Reveal in Finder") { NSWorkspace.shared.activateFileViewerSelecting([selection.media.url]) }
                Button("Done", action: close).keyboardShortcut(.cancelAction)
            }
            if let failure { ContentUnavailableView("Media unavailable", systemImage: "externaldrive.badge.exclamationmark", description: Text(failure)) }
            else {
                GeometryReader { geometry in
                    let fit = PreviewSizing.fit(pixels, into: geometry.size, scale: displayScale)
                    let factor = actualSize ? 1 : fit
                    let size = pixels == .zero ? geometry.size : CGSize(width: pixels.width * factor / displayScale, height: pixels.height * factor / displayScale)
                    ScrollView([.horizontal, .vertical]) {
                        Group {
                            if selection.media.kind == "video" {
                                ZStack {
                                    VideoSurface(player: transport.player)
                                    if transport.player == nil { ProgressView("Opening video…") }
                                }
                            } else if let photo { Image(nsImage: photo).resizable().scaledToFit() }
                            else { ProgressView() }
                        }.frame(width: size.width, height: size.height)
                            .frame(minWidth: geometry.size.width, minHeight: geometry.size.height)
                    }
                    .onAppear { zoom = factor }
                    .onChange(of: factor) { _, value in zoom = value }
                }
                if selection.media.kind == "video" { TransportBar(transport: transport, shortcutsEnabled: !editingMetadata && !showInfo, navigationShortcutsEnabled: false) }
            }
        }
        .padding()
        .task {
            guard FileManager.default.isReadableFile(atPath: selection.media.url.path) else {
                failure = "The original file is offline or inaccessible. Cached previews remain available."
                return
            }
            pixels = await PreviewSizing.dimensions(selection.media)
            guard !Task.isCancelled else { return }
            if selection.media.kind == "video" {
                do {
                    let p = try await preparedPlayer(url: selection.media.url, timestamp: selection.timestamp ?? 0)
                    guard !Task.isCancelled else { p.pause(); return }
                    transport.attach(p)
                    transport.toggle()
                } catch { if !Task.isCancelled { failure = error.localizedDescription } }
            } else {
                photo = await FrameCache.shared.image(selection.media.url.path, maxPixelSize: max(2048, Int(max(pixels.width, pixels.height))))
                if photo == nil { failure = "Could not decode this photo." }
            }
        }
        .onDisappear { transport.stop() }
    }
}

struct LibraryView: View {
    @StateObject private var library: Library
    @StateObject private var clips: ClipSelection
    @ViewState private var showClips = true
    @StateObject private var navigation: BrowserSelection
    private var selection: Selection? {
        get { navigation.preview }
        nonmutating set { navigation.preview = newValue }
    }
    private var focusedMedia: Media? {
        get { navigation.focused }
        nonmutating set { navigation.focused = newValue }
    }
    @ViewState private var commandHover = false
    @ViewState private var nearby = true
    @ViewState private var kind = "all"
    @ViewState private var sort = "default"
    @ViewState private var favoritesOnly = false
    @ViewState private var selectedLabels = Set<String>()
    @ViewState private var showImport = false
    @ViewState private var showDateRange = false
    @ViewState private var showShortcuts = false
    private let importing = Importing.shared
    @StateObject private var annotations = Annotations()
    private let backups = CatalogBackups.shared
    private let indexing = Indexing.shared
    @ObservedObject private var catalogUpdates = CatalogUpdates.shared
    @ViewState private var scrollTarget: String?
    @FocusState private var searchFocused: Bool
    private var visibleItems: [Media] {
        let filtered = library.items.filter {
            let value = annotations.value(for: $0)
            let labels = Set([value.place] + value.tags)
            return (kind == "all" || $0.kind == kind) && (!favoritesOnly || value.favorite)
                && library.matchesFormat($0)
                && library.matchesDevice($0)
                && library.matchesDate($0)
                && (selectedLabels.isEmpty || !labels.isDisjoint(with: selectedLabels))
        }
        if sort == "default" && library.showingMatches { return filtered }
        return filtered.sorted {
            let a = $0.metadata?.sortDate ?? "", b = $1.metadata?.sortDate ?? ""
            if a == b { return $0.path < $1.path }
            if a.isEmpty { return false }; if b.isEmpty { return true }
            return sort == "oldest" ? a < b : a > b
        }
    }
    private var availableLabels: [String] { Set(annotations.values.values.flatMap { $0.tags + [$0.place] }.filter { !$0.isEmpty }).sorted() }
    private func updatePagedFilters() {
        guard library.usesProjection else { return }
        library.browseKind = kind == "all" ? nil : kind
        library.oldestFirst = sort == "oldest"
        if favoritesOnly || !selectedLabels.isEmpty {
            library.browseAssets = Set(annotations.values.compactMap { asset, value in
                let labels = Set(value.tags + [value.place])
                return (!favoritesOnly || value.favorite) && (selectedLabels.isEmpty || !labels.isDisjoint(with: selectedLabels)) ? asset : nil
            })
        } else { library.browseAssets = nil }
        library.search()
    }
    private var hasFilters: Bool { kind != "all" || library.format != .all || library.deviceFilter != "All devices" || library.dateEnabled || library.queryDates != nil || favoritesOnly || !selectedLabels.isEmpty }
    private func clearFilters() {
        kind = "all"; library.format = .all; library.deviceFilter = "All devices"; favoritesOnly = false; selectedLabels = []
        library.dateEnabled = false
        if let parsed = try? DateSearch.parse(library.query) { library.query = parsed.text }
        library.search()
    }
    private func focus(_ media: Media) {
        navigation.select(media, extending: false); searchFocused = false
        NSApp.keyWindow?.makeFirstResponder(nil)
    }
    private func selectCard(_ media: Media) {
        navigation.select(media, extending: NSEvent.modifierFlags.contains(.command))
        searchFocused = false
        NSApp.keyWindow?.makeFirstResponder(nil)
    }
    private var highlightedItems: [Media] { visibleItems.filter { navigation.selectedIDs.contains($0.id) } }
    private func open(_ media: Media, timestamp: Double?) {
        focus(media); navigation.previewItems = nil; selection = Selection(media: media, timestamp: timestamp)
    }
    private func handleKey(_ code: UInt16) -> Bool {
        guard !showImport else { return false }
        if code == 191 { showShortcuts.toggle(); return true }
        if code == 53, selection != nil { selection = nil; return true }
        if code == 11 {
            let targets = selection.map { [$0.media] } ?? highlightedItems
            clips.toggle(targets.isEmpty ? focusedMedia.map { [$0] } ?? [] : targets)
            return true
        }
        if code == 49 {
            if selection != nil { selection = nil }
            else if highlightedItems.count > 1 {
                navigation.previewItems = highlightedItems
                let first = highlightedItems.first { $0.id == focusedMedia?.id } ?? highlightedItems[0]
                selection = Selection(media: first, timestamp: first.match.timestamp)
            }
            else if let item = highlightedItems.first { open(item, timestamp: item.match.timestamp) }
            else if let item = focusedMedia, visibleItems.contains(where: { $0.id == item.id }) { open(item, timestamp: item.match.timestamp) }
            else if let item = visibleItems.first { open(item, timestamp: item.match.timestamp) }
            return true
        }
        guard [123, 124, 125, 126].contains(code) else { return false }
        if selection != nil, [125, 126].contains(code) { return true }
        let items = selection != nil ? navigation.previewItems ?? visibleItems : visibleItems
        guard !items.isEmpty else { return false }
        let offset = (code == 123 || code == 126) ? -1 : 1
        let current = selection?.media ?? focusedMedia
        let index = current.flatMap { item in items.firstIndex { $0.id == item.id } }
        let target = index.map {
            selection == nil ? GridNavigation.target(index: $0, key: code, count: items.count, columns: navigation.columns)
                : max(0, min(items.count - 1, $0 + offset))
        } ?? 0
        if selection != nil {
            guard index != target else { NSSound.beep(); return true }
            focusedMedia = items[target]
            if navigation.previewItems == nil { navigation.selectedIDs = [items[target].id] }
            selection = Selection(media: items[target], timestamp: items[target].match.timestamp)
        } else { focus(items[target]); scrollTarget = items[target].id }
        return true
    }
    private func previewPosition(_ selection: Selection, items: [Media]) -> String {
        let scoped = navigation.previewItems ?? items
        guard let index = scoped.firstIndex(where: { $0.id == selection.media.id }) else { return "" }
        return "\(index + 1) / \(scoped.count)"
    }
    private func adjacent(to selection: Selection, offset: Int) -> Media? {
        let items = visibleItems
        guard let index = items.firstIndex(where: { $0.id == selection.media.id }), items.indices.contains(index + offset) else { return nil }
        return items[index + offset]
    }
    @MainActor init(library: Library? = nil, clips: ClipSelection? = nil, navigation: BrowserSelection? = nil) {
        _library = StateObject(wrappedValue: library ?? Library())
        _clips = StateObject(wrappedValue: clips ?? ClipSelection())
        _navigation = StateObject(wrappedValue: navigation ?? BrowserSelection())
    }
    var body: some View {
        let displayed = visibleItems
        let positions = Dictionary(uniqueKeysWithValues: displayed.enumerated().map { ($0.element.id, $0.offset) })
        VStack(spacing: 0) {
            VStack(spacing: 14) {
                HStack {
                    Text("Mami").font(.system(size: 25, weight: .semibold, design: .rounded))
                    Spacer()
                    Button { showShortcuts.toggle() } label: { Image(systemName: "questionmark.circle") }
                        .help("Keyboard shortcuts (?)").accessibilityLabel("Keyboard shortcuts")
                        .popover(isPresented: $showShortcuts) { ShortcutHelp { showShortcuts = false } }
                    Button("Import media…") { showImport = true }
                    Button { showClips.toggle() } label: { Label("\(clips.items.count)", systemImage: "sidebar.right") }
                        .help("Show or hide selected clips").accessibilityLabel("Selected clips, \(clips.items.count)")
                }
                HStack(spacing: 14) {
                    Image(systemName: "magnifyingglass").font(.title2).foregroundStyle(.secondary)
                    TextField(library.mode == "speech" ? "Find words spoken in a video…" : "What are you looking for?", text: $library.query)
                        .font(.system(size: 21)).textFieldStyle(.plain).onSubmit { library.search() }
                        .focused($searchFocused)
                        .accessibilityLabel("Search your media")
                        .task(id: library.query) {
                            do { try await Task.sleep(for: SearchTiming.typingDebounce) } catch { return }
                            guard library.ready, !Task.isCancelled else { return }
                            library.search()
                        }
                    if !library.query.isEmpty {
                        Button { library.query = ""; library.search() } label: { Image(systemName: "xmark.circle.fill") }
                            .buttonStyle(.plain).foregroundStyle(.secondary).help("Clear search")
                    }
                    ProgressView().controlSize(.small).frame(width: 18, height: 18).opacity(library.searching ? 1 : 0)
                    Button("Search") { library.search() }.buttonStyle(.borderedProminent).controlSize(.large)
                        .disabled(!library.ready || library.searching)
                }
                .padding(.horizontal, 20).padding(.vertical, 15)
                .background(Color(red: 0.16, green: 0.17, blue: 0.20), in: RoundedRectangle(cornerRadius: 16))
                .overlay(RoundedRectangle(cornerRadius: 16).stroke(Color.white.opacity(0.12), lineWidth: 1))
                .frame(maxWidth: 820).frame(maxWidth: .infinity)
                if library.speechAvailable {
                    Picker("Search", selection: $library.mode) {
                        Text("Visuals & speech").tag("both")
                        Text("Visuals only").tag("visual")
                        Text("Speech only").tag("speech")
                    }.pickerStyle(.segmented).frame(maxWidth: 440)
                        .accessibilityLabel("Search content")
                        .onChange(of: library.mode) { _, _ in library.search() }
                }
            }.padding(.horizontal, 24).padding(.top, 20).padding(.bottom, 18)
            HStack {
                    Text(library.usesProjection && !library.showingMatches ? "\(library.totalMediaCount) media · \(displayed.count) loaded" : "\(displayed.count) media").monospacedDigit()
                Spacer()
            }.font(.caption).padding(.horizontal, 14).padding(.bottom, 10)
            HStack {
                Picker("Show", selection: $kind) {
                    Text("All media").tag("all")
                    Text("Videos").tag("video")
                    Text("Photos").tag("image")
                }.pickerStyle(.segmented).labelsHidden().frame(width: 220)
                Divider().frame(height: 20)
                ForEach([MediaFormat.vertical, .horizontal]) { shape in
                    Toggle(isOn: Binding(get: { library.format == shape }, set: { library.format = $0 ? shape : .all })) {
                        Label(shape.label, systemImage: shape == .vertical ? "rectangle.portrait" : "rectangle")
                    }
                        .toggleStyle(.button)
                }
                Spacer()
                Toggle(isOn: $favoritesOnly) { Image(systemName: "heart.fill") }.toggleStyle(.button).help("Show favorites only").accessibilityLabel("Favorites only")
            }.padding(.horizontal, 24).padding(.bottom, 10)
            HStack {
                TagFilterPicker(available: availableLabels, selected: $selectedLabels)
                Button { showDateRange = true } label: {
                    Label(library.dateEnabled ? DateFilterDraft.label(from: library.dateFrom, through: library.dateThrough) : "Date", systemImage: "calendar")
                }
                    .tint(library.dateEnabled ? Color.accentColor : Color.secondary)
                    .popover(isPresented: $showDateRange) {
                        DateFilterPopover(from: library.dateFrom, through: library.dateThrough, enabled: library.dateEnabled, earliest: library.earliestCaptureDate,
                            apply: { from, through in
                                library.dateFrom = from; library.dateThrough = through
                                library.dateEnabled = true; library.search(); showDateRange = false
                            }, clear: { library.dateEnabled = false; library.search(); showDateRange = false },
                            cancel: { showDateRange = false })
                    }
                Picker("Camera", selection: $library.deviceFilter) {
                    Text("All devices").tag("All devices")
                    ForEach(library.devices, id: \.self) { Text($0).tag($0) }
                }.labelsHidden().frame(maxWidth: 210).onChange(of: library.deviceFilter) { _, _ in library.search() }
                if favoritesOnly { Text("Favorites").font(.caption).foregroundStyle(.pink) }
                if hasFilters { Button("Clear filters") { clearFilters() }.font(.caption) }
                Spacer()
                if library.showingMatches {
                    Toggle("Scrub ±8 seconds around match", isOn: $nearby).toggleStyle(.switch).controlSize(.small)
                }
                Picker("Sort", selection: $sort) {
                    Text(library.showingMatches ? "Best match" : "Newest first").tag("default")
                    Text("Date: newest").tag("newest")
                    Text("Oldest first").tag("oldest")
                }.labelsHidden().frame(width: 135)
            }.controlSize(.regular).padding(.horizontal, 24).padding(.bottom, 12)
            if let error = library.error { Text(error).foregroundStyle(.red).padding() }
            if let error = annotations.error { Text(error).foregroundStyle(.red).font(.caption).padding() }
            Divider()
            HStack(spacing: 0) {
            ScrollViewReader { proxy in
            ScrollView {
                if displayed.isEmpty, library.ready, !library.searching {
                    ContentUnavailableView {
                        Label(hasFilters ? "No media fits these filters" : "No search results", systemImage: "magnifyingglass")
                    } description: {
                        Text(hasFilters ? "Try another shape or clear the filters to see more of your library." : "Try fewer words. Speech search finds Romanian words within a single spoken passage; accents are optional.")
                    } actions: {
                        if hasFilters { Button("Clear filters") { clearFilters() } }
                    }
                }
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 240, maximum: 360), spacing: 14)], spacing: 14) {
                    ForEach(displayed) { media in
                        MediaCard(media: media, projection: library.projectionReader, nearby: (nearby && library.showingMatches) != commandHover, annotations: annotations, clips: clips,
                                    focused: navigation.selectedIDs.contains(media.id), select: { selectCard(media) },
                                     dragItems: { navigation.itemsForDrag(media, in: displayed) }, dragEnabled: selection == nil && !showImport, open: { open($0, timestamp: $1) }).id(media.id)
                            .onAppear {
                                if media.id == library.items.last?.id { Task { await library.loadMore() } }
                            }
                            .task(id: media.match.cacheKey) {
                                guard let position = positions[media.id] else { return }
                                let margin = max(3, navigation.columns * 2)
                                for index in max(0, position - margin)..<min(displayed.count, position + margin + 1) {
                                    guard !Task.isCancelled else { return }
                                    _ = await FrameCache.shared.image(displayed[index].match.frame, crop: displayed[index].match.crop)
                                }
                            }
                    }
                }
                .onGeometryChange(for: Int.self) { geometry in
                    max(1, Int((geometry.size.width + 14) / (240 + 14)))
                } action: { navigation.columns = $0 }
                .padding(14)
            }.frame(maxWidth: .infinity, maxHeight: .infinity)
                .onChange(of: scrollTarget) { _, id in
                    if let id { proxy.scrollTo(id, anchor: .center) }
                }
            }
            if showClips {
                SelectionIsland(clips: clips, projection: library.projectionReader, open: { clip in
                    let media = library.catalogMedia.first { $0.assetID == clip.assetID } ?? clip.media
                    open(media, timestamp: clip.timestamp)
                }, collapse: { showClips = false })
            }
            }.frame(maxWidth: .infinity, maxHeight: .infinity)
            LibraryFooter(library: library)
        }
        .frame(minWidth: 850, minHeight: 600)
        .background(Color(red: 0.08, green: 0.09, blue: 0.11))
        .preferredColorScheme(.dark)
        .environment(\.locale, Locale(identifier: "en_US"))
        .background(BrowserKeys(command: { commandHover = $0 }, key: handleKey, mouse: { point, size in
            guard selection != nil, !showImport,
                  !CGRect(origin: .zero, size: size).insetBy(dx: 20, dy: 20).contains(point) else { return false }
            selection = nil
            return true
        }).frame(width: 0, height: 0))
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didResignActiveNotification)) { _ in commandHover = false }
        .onChange(of: library.format) { _, _ in library.search() }
        .onChange(of: kind) { _, _ in updatePagedFilters() }
        .onChange(of: sort) { _, _ in updatePagedFilters() }
        .onChange(of: favoritesOnly) { _, _ in updatePagedFilters() }
        .onChange(of: selectedLabels) { _, _ in updatePagedFilters() }
        .onChange(of: annotations.values) { _, _ in if favoritesOnly || !selectedLabels.isEmpty { updatePagedFilters() } }
        .onChange(of: library.ready) { _, ready in if ready { updatePagedFilters() } }
        .background(Button("Focus search") { searchFocused = true }.keyboardShortcut("f", modifiers: .command).hidden())
        .task { await library.load() }
        .task { await annotations.load() }
        .task { await clips.load() }
        .task { if !LaunchMode.isIntegrationCheck { PhotosImporting.shared.startAutomatic() } }
        .task { if !LaunchMode.isIntegrationCheck { Importing.shared.startAutomatic() } }
        .task {
            while !Task.isCancelled {
                do { try await Task.sleep(for: .seconds(60)) } catch { return }
                backups.schedule()
            }
        }
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.willTerminateNotification)) { _ in importing.shutdown(); indexing.stop(); Indexing.previews.stop(); SearchMaintenance.shared.stop(); backups.flush() }
        .task(id: catalogUpdates.generation) {
            if catalogUpdates.generation > 0 {
                do { try await Task.sleep(for: .milliseconds(100)) } catch { return }
                await library.refreshCatalog()
                guard !Task.isCancelled else { return }
                clips.reconnect(library.catalogMedia)
            }
        }
        .overlay {
            if let item = selection {
                GeometryReader { geometry in
                    ZStack {
                        Color.black.opacity(0.72).contentShape(Rectangle()).onTapGesture { selection = nil }
                        Playback(selection: item, annotations: annotations, clips: clips, close: { selection = nil },
                                 position: previewPosition(item, items: displayed))
                            .id(item.id)
                            .frame(width: max(1, geometry.size.width - 40), height: max(1, geometry.size.height - 40))
                            .background(Color(red: 0.08, green: 0.09, blue: 0.11), in: RoundedRectangle(cornerRadius: 14))
                            .contentShape(Rectangle()).onTapGesture { }
                    }.frame(width: geometry.size.width, height: geometry.size.height)
                }
            }
        }
        .sheet(isPresented: $showImport) { ImportSheet() }
    }
}

struct MamiApp: App {
    var body: some Scene {
        WindowGroup("Mami") { LibraryView() }
            .defaultSize(width: 1200, height: 800)
        Settings { MamiSettings() }
    }
}

@main enum EntryPoint {
    @MainActor static func main() {
        if CommandLine.arguments.contains("--worker-pipe-test") {
            do { try WorkerPipe.check(); exit(0) }
            catch { fputs("WORKER PIPE TEST FAILED: \(error)\n", stderr); exit(1) }
        }
        if CommandLine.arguments.count == 3, CommandLine.arguments[1] == "--encode-text" {
            do {
                try NativeTextEncoder.serve(modelURL: URL(fileURLWithPath: CommandLine.arguments[2]))
                exit(0)
            } catch { fputs("\(error)\n", stderr); exit(1) }
        }
        if CommandLine.arguments.count == 4, CommandLine.arguments[1] == "--video-previews" {
            do {
                try VideoPreviews.generate(source: CommandLine.arguments[2], manifest: CommandLine.arguments[3])
                exit(0)
            } catch { fputs("\(error)\n", stderr); exit(1) }
        }
        if CommandLine.arguments.count >= 3, CommandLine.arguments[1] == "--image-probe" || CommandLine.arguments[1] == "--image-frame" {
            do {
                if CommandLine.arguments[1] == "--image-probe" {
                    let data = try JSONSerialization.data(withJSONObject: ImageDecoding.metadata(CommandLine.arguments[2]))
                    print(String(decoding: data, as: UTF8.self))
                } else {
                    guard CommandLine.arguments.count == 4 else { throw AppError.message("Image preview requires a target") }
                    try ImageDecoding.preview(CommandLine.arguments[2], to: CommandLine.arguments[3])
                }
                exit(0)
            } catch { fputs("\(error)\n", stderr); exit(1) }
        }
        if let index = CommandLine.arguments.firstIndex(of: "--persistent-search-test"), CommandLine.arguments.indices.contains(index + 1) {
            NSApplication.shared.setActivationPolicy(.accessory)
            Task {
                do { try await checkPersistentSearch(at: URL(fileURLWithPath: CommandLine.arguments[index+1])); exit(0) }
                catch { print("PERSISTENT SEARCH TEST FAILED: \(error)"); exit(1) }
            }
            NSApplication.shared.run()
            return
        }
        if let index = CommandLine.arguments.firstIndex(of: "--photos-memory-test"), CommandLine.arguments.indices.contains(index + 1) {
            do { try PhotosMemoryCheck.run(file: URL(fileURLWithPath: CommandLine.arguments[index + 1])); exit(0) }
            catch { print("PHOTOS MEMORY FAILED: \(error)"); exit(1) }
        }
        if let index = CommandLine.arguments.firstIndex(of: "--scan-ui-test"), CommandLine.arguments.indices.contains(index + 1) {
            NSApplication.shared.setActivationPolicy(.accessory)
            Task {
                do { try await scanUITest(URL(fileURLWithPath: CommandLine.arguments[index + 1])); exit(0) }
                catch { Indexing.shared.stop(); Indexing.previews.stop(); print("SCAN UI TEST FAILED: \(error)"); exit(1) }
            }
            NSApplication.shared.run()
        } else if let index = CommandLine.arguments.firstIndex(of: "--catalog-test"), CommandLine.arguments.indices.contains(index + 1) {
            do { try checkCatalog(at: URL(fileURLWithPath: CommandLine.arguments[index + 1])); exit(0) }
            catch { print("CATALOG TEST FAILED: \(error)"); exit(1) }
        } else if let index = CommandLine.arguments.firstIndex(of: "--ui-test"), CommandLine.arguments.indices.contains(index + 1) {
            NSApplication.shared.setActivationPolicy(.accessory)
            Task {
                do { try await uiTest(URL(fileURLWithPath: CommandLine.arguments[index + 1])); exit(0) }
                catch { print("UI TEST FAILED: \(error)"); exit(1) }
            }
            NSApplication.shared.run()
        } else if CommandLine.arguments.contains("--self-test") {
            NSApplication.shared.setActivationPolicy(.accessory)
            Task {
                do { try await selfTest(); exit(0) }
                catch { print("SELF-TEST FAILED: \(error)"); exit(1) }
            }
            NSApplication.shared.run()
        } else {
            MamiApp.main()
        }
    }

    @MainActor static func scanUITest(_ directory: URL) async throws {
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
        let indexing = Indexing.shared
        let host = NSHostingView(rootView: IndexingBar().padding().frame(width: 1000).preferredColorScheme(.dark))
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1050, height: 160), styleMask: [.titled], backing: .buffered, defer: false)
        window.contentView = host
        window.orderFrontRegardless()
        func until(_ condition: () -> Bool, seconds: Double = 30) async throws {
            let deadline = Date().addingTimeInterval(seconds)
            while !condition() {
                if let error = indexing.error { throw AppError.message(error) }
                guard Date() < deadline else { throw AppError.message("Timed out waiting for scanner: \(indexing.phase)") }
                try await Task.sleep(for: .milliseconds(50))
            }
        }
        func snapshot(_ name: String) throws {
            host.layoutSubtreeIfNeeded()
            guard let bitmap = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { throw AppError.message("Cannot capture scan controls") }
            host.cacheDisplay(in: host.bounds, to: bitmap)
            try bitmap.representation(using: .png, properties: [:])?.write(to: directory.appendingPathComponent(name), options: .withoutOverwriting)
        }
        Indexing.startAll()
        try await until { indexing.phase == "Checking media" || indexing.phase == "Up to date" || indexing.paused }
        if !indexing.paused { indexing.togglePause() }
        try await until { indexing.paused && indexing.waiting }
        try snapshot("paused.png")
        let count = try SQLDatabase(Catalog.standard.database, readOnly: true).scalar("SELECT count(*) FROM scan_files")
        try await Task.sleep(for: .milliseconds(500))
        guard try SQLDatabase(Catalog.standard.database, readOnly: true).scalar("SELECT count(*) FROM scan_files") == count else {
            throw AppError.message("Scanner continued writing while paused")
        }
        indexing.togglePause()
        try await until { !indexing.paused }
        try snapshot("scanning.png")
        try await until({ indexing.phase == "Up to date" }, seconds: 900)
        let rows = try SQLDatabase(Catalog.standard.database, readOnly: true).scalar("SELECT count(*) FROM scan_files")
        guard rows == "498" else { throw AppError.message("Expected 498 scanned files, got \(rows)") }
        _ = try Catalog.standard.snapshotIfChanged()
        try snapshot("completed.png")
        indexing.stop()
        Indexing.previews.stop()
        window.orderOut(nil)
        print("SCAN UI TEST PASSED: native progress, persisted pause, no writes while paused, resume and 498-file automatic scan")
    }

    @MainActor static func uiTest(_ directory: URL) async throws {
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        let library = Library()
        await library.load()
        guard library.ready else { throw AppError.message(library.error ?? "Library not ready") }
        guard library.mode == "both" else { throw AppError.message("Combined search is not the default") }
        guard library.items.allSatisfy({ $0.metadata != nil }) else { throw AppError.message("Missing capture metadata") }
        if !ContentIdentity.locations.isEmpty {
            guard library.items.allSatisfy({ FileManager.default.isReadableFile(atPath: $0.url.path) && $0.assetID.hasPrefix("sha256:") }) else {
                throw AppError.message("Relocated original is unavailable or lost its content identity")
            }
            print("RELOCATION all \(library.items.count) originals readable with content identities")
        }
        let annotationTest = Annotations(directory: directory.appendingPathComponent("annotation-test"))
        await annotationTest.load()
        let firstMedia = library.items[0]
        annotationTest.save(Annotation(favorite: true, tags: ["Test tag"], place: "Test place"), for: firstMedia)
        let restored = try Annotations.read(annotationTest.directory)
        guard restored[firstMedia.assetID]?.favorite == true, restored[firstMedia.assetID]?.tags == ["Test tag"] else {
            throw AppError.message("Annotation persistence failed")
        }
        annotationTest.toggleFavorite(firstMedia)
        let restoredAgain = try Annotations.read(annotationTest.directory)
        guard restoredAgain[firstMedia.assetID]?.favorite == false, restoredAgain[firstMedia.assetID]?.place == "Test place",
               try SQLDatabase(annotationTest.catalog.userDatabase, readOnly: true).scalar("SELECT count(*) FROM annotation_history") == "2" else {
            throw AppError.message("Annotation history was not preserved")
        }
        let selectionCatalog = Catalog(directory: directory.appendingPathComponent("selection-check"))
        let receiptURL = directory.appendingPathComponent("photos-resource-fixture.json")
        let receipt = PhotosExporter.Receipt(file: "no-longer-local.mov", digest: "verified-fixture-digest", size: 123, imported: false)
        try JSONEncoder().encode(receipt).write(to: receiptURL)
        try PhotosExporter.markImported([receiptURL], catalog: selectionCatalog)
        try FileManager.default.removeItem(at: receiptURL)
        guard try selectionCatalog.photosHistory().contains("photos-resource-fixture") else {
            throw AppError.message("Photos import history depended on staging or original location")
        }
        let selectedMedia = Array(library.items.prefix(2))
        try selectionCatalog.synchronize(selectedMedia)
        let clips = ClipSelection(catalog: selectionCatalog)
        await clips.load()
        selectedMedia.forEach { clips.toggle($0) }
        let savedSelection = try selectionCatalog.snapshotIfChanged()
        clips.save(clips.items)
        guard try selectionCatalog.snapshotIfChanged().file == savedSelection.file else { throw AppError.message("Unchanged selection created a backup") }
        let reopened = ClipSelection(catalog: selectionCatalog)
        await reopened.load()
        guard reopened.items == clips.items, reopened.items.count == 2 else { throw AppError.message("Selected clips did not survive reload") }
        let pasteboard = NSPasteboard(name: NSPasteboard.Name("mami-selection-test-\(UUID().uuidString)"))
        let writers = try ClipDragHandle.writers(clips.items)
        guard pasteboard.writeObjects(writers), pasteboard.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true])?.count == 2 else {
            throw AppError.message("Selection did not provide two native file drag items")
        }
        let selectedRestore = Catalog(directory: directory.appendingPathComponent("selection-restored"))
        try FileManager.default.createDirectory(at: selectedRestore.directory, withIntermediateDirectories: true)
        try FileManager.default.copyItem(at: selectionCatalog.backups.appendingPathComponent(savedSelection.file), to: selectedRestore.database)
        guard try selectedRestore.selectedClips() == clips.items else { throw AppError.message("Selection snapshot restore failed") }
        guard try selectedRestore.photosHistory().contains("photos-resource-fixture") else { throw AppError.message("Photos import history backup restore failed") }
        print("PHOTOS durable resource identity survives absent originals, staging removal and catalog restore")
        let pipelineCancellation = PhotosCancellation()
        let pipeline = PhotosPipeline(cancellation: pipelineCancellation)
        let pipelineProducer = Task.detached {
            for index in 0..<20 { try pipeline.enqueue(URL(fileURLWithPath: "/fixture/\(index)"), size: 128 * 1024 * 1024) }
            pipeline.finish()
        }
        var consumed = 0
        while let batch = try await Task.detached(operation: { try pipeline.take() }).value {
            guard batch.count <= 4 else { throw AppError.message("Photos pipeline exceeded byte bound") }
            consumed += batch.count
            pipeline.acknowledge(batch)
        }
        try await pipelineProducer.value
        guard consumed == 20 else { throw AppError.message("Photos pipeline lost work") }
        print("PHOTOS concurrent bounded producer/consumer passed")
        clips.move(clips.items[1].id, by: -1)
        guard clips.items.first?.assetID == selectedMedia[1].assetID else { throw AppError.message("Selection ordering failed") }
        print("SELECTION persistence, no-op backup, snapshot restore, ordering and two native file drag items passed")
        let navigation = BrowserSelection()
        let host = NSHostingView(rootView: LibraryView(library: library, clips: clips, navigation: navigation))
        host.appearance = NSAppearance(named: .darkAqua)
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1200, height: 800),
                              styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
        window.contentView = host
        window.orderFrontRegardless()
        func snapshot(_ name: String) throws {
            host.layoutSubtreeIfNeeded()
            guard let bitmap = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { throw AppError.message("Cannot capture app view") }
            host.cacheDisplay(in: host.bounds, to: bitmap)
            guard let png = bitmap.representation(using: .png, properties: [:]) else { throw AppError.message("Cannot encode app view") }
            try png.write(to: directory.appendingPathComponent(name), options: .withoutOverwriting)
        }
        try await Task.sleep(for: .seconds(1))
        try snapshot("library.png")
        let settingsHost = NSHostingView(rootView: MamiSettings())
        settingsHost.appearance = NSAppearance(named: .darkAqua)
        let settingsWindow = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 580, height: 520),
                                      styleMask: [.titled], backing: .buffered, defer: false)
        settingsWindow.contentView = settingsHost
        settingsWindow.orderFrontRegardless()
        try await Task.sleep(for: .milliseconds(300))
        settingsHost.layoutSubtreeIfNeeded()
        if let bitmap = settingsHost.bitmapImageRepForCachingDisplay(in: settingsHost.bounds) {
            settingsHost.cacheDisplay(in: settingsHost.bounds, to: bitmap)
            try bitmap.representation(using: .png, properties: [:])?.write(to: directory.appendingPathComponent("settings.png"))
        }
        settingsWindow.orderOut(nil)
        var dateCalendar = Calendar(identifier: .gregorian)
        dateCalendar.timeZone = TimeZone(identifier: "Europe/Bucharest")!
        dateCalendar.firstWeekday = 2
        func filterDay(_ year: Int, _ month: Int, _ day: Int) -> Date {
            dateCalendar.date(from: DateComponents(year: year, month: month, day: day))!
        }
        func checkFilterDates(_ dates: (Date, Date), _ from: Date, _ through: Date) throws {
            guard dates.0 == from, dates.1 == through else { throw AppError.message("Date filter calendar boundary failed") }
        }
        let filterNow = filterDay(2024, 3, 31)
        try checkFilterDates(DateFilterPreset.today.dates(now: filterNow, calendar: dateCalendar), filterNow, filterNow)
        try checkFilterDates(DateFilterPreset.yesterday.dates(now: filterDay(2024, 3, 1), calendar: dateCalendar), filterDay(2024, 2, 29), filterDay(2024, 2, 29))
        try checkFilterDates(DateFilterPreset.seven.dates(now: filterDay(2024, 4, 1), calendar: dateCalendar), filterDay(2024, 3, 26), filterDay(2024, 4, 1))
        try checkFilterDates(DateFilterPreset.thirty.dates(now: filterNow, calendar: dateCalendar), filterDay(2024, 3, 2), filterNow)
        try checkFilterDates(DateFilterPreset.month.dates(now: filterDay(2024, 2, 10), calendar: dateCalendar), filterDay(2024, 2, 1), filterDay(2024, 2, 29))
        var draft = DateFilterDraft(from: filterNow, through: filterNow)
        draft.switchMode(.week, now: filterNow, calendar: dateCalendar)
        try checkFilterDates((draft.from, draft.through), filterDay(2024, 3, 25), filterNow)
        draft.switchMode(.months, now: filterDay(2024, 2, 10), calendar: dateCalendar)
        try checkFilterDates((draft.from, draft.through), filterDay(2024, 2, 1), filterDay(2024, 2, 29))
        guard draft.anchor == nil else { throw AppError.message("Default month must not anchor a new range") }
        draft.switchMode(.year, now: filterNow, calendar: dateCalendar)
        try checkFilterDates((draft.from, draft.through), filterDay(2024, 1, 1), filterDay(2024, 12, 31))
        draft.switchMode(.day, now: filterNow, calendar: dateCalendar)
        try checkFilterDates((draft.from, draft.through), filterNow, filterNow)
        draft.select(filterNow, mode: .week, calendar: dateCalendar)
        try checkFilterDates((draft.from, draft.through), filterDay(2024, 3, 25), filterNow)
        draft.select(filterNow, mode: .day, calendar: dateCalendar)
        try checkFilterDates((draft.from, draft.through), filterNow, filterNow)
        draft.select(filterDay(2024, 2, 10), mode: .months, calendar: dateCalendar)
        try checkFilterDates((draft.from, draft.through), filterDay(2024, 2, 1), filterDay(2024, 2, 29))
        draft.select(filterDay(2023, 12, 10), mode: .months, calendar: dateCalendar)
        try checkFilterDates((draft.from, draft.through), filterDay(2023, 12, 1), filterDay(2024, 2, 29))
        draft.select(filterNow, mode: .year, calendar: dateCalendar)
        try checkFilterDates((draft.from, draft.through), filterDay(2024, 1, 1), filterDay(2024, 12, 31))
        draft.select(filterDay(2024, 4, 5), mode: .range, calendar: dateCalendar)
        draft.select(filterDay(2024, 3, 29), mode: .range, calendar: dateCalendar)
        try checkFilterDates((draft.from, draft.through), filterDay(2024, 3, 29), filterDay(2024, 4, 5))
        let dateHost = NSHostingView(rootView: DateFilterPopover(from: filterDay(2026, 9, 7), through: filterDay(2026, 9, 20), enabled: true, earliest: filterDay(2026, 1, 1), apply: { _, _ in }, clear: {}, cancel: {}))
        dateHost.appearance = NSAppearance(named: .darkAqua)
        let dateWindow = NSWindow(contentRect: NSRect(x: 40, y: 40, width: 740, height: 540), styleMask: [.titled], backing: .buffered, defer: false)
        dateWindow.contentView = dateHost
        dateWindow.makeKeyAndOrderFront(nil)
        try await Task.sleep(for: .milliseconds(250))
        if let bitmap = dateHost.bitmapImageRepForCachingDisplay(in: dateHost.bounds) {
            dateHost.cacheDisplay(in: dateHost.bounds, to: bitmap)
            try bitmap.representation(using: .png, properties: [:])?.write(to: directory.appendingPathComponent("date-filter.png"))
        }
        dateWindow.orderOut(nil)
        print("DATE FILTER presets, leap day, DST, calendar week, single/multiple months, year and reverse range passed")
        let customStart = Date(timeIntervalSince1970: 1_750_000_000)
        let customPredicate = PhotosExporter.fetchOptions(range: DateInterval(start: customStart, end: .distantFuture)).predicate!
        guard customPredicate.evaluate(with: ["creationDate": customStart]),
              customPredicate.evaluate(with: ["creationDate": customStart.addingTimeInterval(400 * 86400)]),
              !customPredicate.evaluate(with: ["creationDate": customStart.addingTimeInterval(-1)]) else {
            throw AppError.message("Configured Photos start date was not inclusive and open-ended")
        }
        let range = PhotosExporter.yearRange()
        guard let newestFirst = PhotosExporter.fetchOptions(range: range).sortDescriptors?.first,
              newestFirst.key == "creationDate", !newestFirst.ascending else {
            throw AppError.message("Photos must fetch newest capture dates first")
        }
        let predicate = PhotosExporter.fetchOptions(range: range).predicate!
        guard predicate.evaluate(with: ["creationDate": range.start]),
              predicate.evaluate(with: ["creationDate": range.end.addingTimeInterval(-1)]),
              !predicate.evaluate(with: ["creationDate": range.start.addingTimeInterval(-1)]),
              !predicate.evaluate(with: ["creationDate": range.end]),
              !predicate.evaluate(with: [:]) else { throw AppError.message("Photos year boundaries failed") }
        guard PreviewSizing.fit(CGSize(width: 4000, height: 2000), into: CGSize(width: 1000, height: 800), scale: 2) == 0.5,
              PreviewSizing.fit(CGSize(width: 100, height: 100), into: CGSize(width: 1000, height: 800), scale: 2) == 1 else {
            throw AppError.message("Preview pixel sizing failed")
        }
        window.makeKeyAndOrderFront(nil)
        app.activate(ignoringOtherApps: true)
        window.makeFirstResponder(nil)
        navigation.focused = library.items.first { $0.kind == "image" }
        func browserKey(_ code: UInt16, characters: String) async throws {
            let event = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: [], timestamp: 0, windowNumber: window.windowNumber,
                                        context: nil, characters: characters, charactersIgnoringModifiers: characters, isARepeat: false, keyCode: code)!
            NSApp.postEvent(event, atStart: false)
            try await Task.sleep(for: .milliseconds(400))
        }
        try await browserKey(49, characters: " ")
        guard navigation.preview?.media.id == navigation.focused?.id, navigation.preview != nil else { throw AppError.message("Space did not open selected preview") }
        try await Task.sleep(for: .seconds(1))
        try snapshot("large-preview.png")
        let previewID = navigation.preview!.media.id
        try await browserKey(123, characters: "\u{f702}")
        if navigation.preview?.media.id == previewID { try await browserKey(124, characters: "\u{f703}") }
        guard navigation.preview?.media.id != previewID else { throw AppError.message("Arrows did not navigate preview") }
        for type in [NSEvent.EventType.leftMouseDown, .leftMouseUp] {
            // Dispatch directly: postEvent substitutes the locked session's
            // hardware cursor location for synthetic mouse coordinates.
            let point = host.convert(NSPoint(x: 12, y: 400), to: nil)
            let event = NSEvent.mouseEvent(with: type, location: point, modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime,
                                          windowNumber: window.windowNumber, context: nil, eventNumber: 0, clickCount: 1, pressure: type == .leftMouseDown ? 1 : 0)!
            app.sendEvent(event)
        }
        try await Task.sleep(for: .milliseconds(300))
        guard navigation.preview == nil else { throw AppError.message("Click outside did not dismiss preview") }
        try await browserKey(49, characters: " ")
        guard navigation.preview != nil else { throw AppError.message("Space did not reopen preview") }
        try await browserKey(49, characters: " ")
        guard navigation.preview == nil, navigation.focused != nil else { throw AppError.message("Space did not close preview and retain selection") }
        print("BROWSING Space toggle, arrow navigation, outside-click dismissal, original-pixel sizing and Photos year-boundary filtering passed")
        navigation.focused = nil
        try await browserKey(124, characters: "\u{f703}")
        let firstGridItem = navigation.focused?.id
        try await browserKey(125, characters: "\u{f701}")
        let belowGridItem = navigation.focused?.id
        guard belowGridItem != firstGridItem else { throw AppError.message("Down did not move to the next grid row") }
        for _ in 0..<navigation.columns { try await browserKey(123, characters: "\u{f702}") }
        guard navigation.focused?.id == firstGridItem else { throw AppError.message("Down moved by the wrong number of columns") }
        for _ in 0..<navigation.columns { try await browserKey(124, characters: "\u{f703}") }
        guard navigation.focused?.id == belowGridItem else { throw AppError.message("Grid row movement was inconsistent") }
        try await browserKey(126, characters: "\u{f700}")
        guard navigation.focused?.id == firstGridItem,
              GridNavigation.target(index: 1, key: 126, count: 8, columns: 3) == 1,
              GridNavigation.target(index: 5, key: 125, count: 8, columns: 3) == 7,
              GridNavigation.target(index: 6, key: 125, count: 8, columns: 3) == 6 else {
            throw AppError.message("Grid navigation boundaries failed")
        }
        print("GRID up/down move by \(navigation.columns) columns; first and partial last rows passed")
        let picks = Array(library.items.prefix(3))
        navigation.select(picks[0], extending: false)
        navigation.select(picks[2], extending: true)
        navigation.select(picks[1], extending: true)
        navigation.select(picks[1], extending: true)
        guard navigation.selectedIDs == Set([picks[0].id, picks[2].id]) else { throw AppError.message("Command-selection toggle failed") }
        try await browserKey(49, characters: " ")
        guard let selectedPreview = navigation.previewItems, selectedPreview.count == 2 else { throw AppError.message("Multiple selection did not scope preview") }
        let firstSelected = selectedPreview[0], lastSelected = selectedPreview[1]
        try await browserKey(123, characters: "\u{f702}")
        guard navigation.preview?.media.id == firstSelected.id else { throw AppError.message("Left did not stay within selected preview") }
        let firstPreviewID = navigation.preview?.id
        try await browserKey(123, characters: "\u{f702}")
        guard navigation.preview?.id == firstPreviewID else { throw AppError.message("Preview reloaded at first boundary") }
        try await browserKey(125, characters: "\u{f701}")
        guard navigation.preview?.media.id == firstSelected.id else { throw AppError.message("Down navigated multi-preview") }
        try await browserKey(124, characters: "\u{f703}")
        guard navigation.preview?.media.id == lastSelected.id else { throw AppError.message("Right did not stay within selected preview") }
        let lastPreviewID = navigation.preview?.id
        try await browserKey(124, characters: "\u{f703}")
        guard navigation.preview?.id == lastPreviewID else { throw AppError.message("Preview reloaded at last boundary") }
        try await browserKey(49, characters: " ")
        clips.clear()
        try await browserKey(11, characters: "b")
        guard Set(clips.items.map(\.assetID)) == Set([picks[0].assetID, picks[2].assetID]) else { throw AppError.message("B did not add multi-selection") }
        try await browserKey(11, characters: "b")
        guard clips.items.isEmpty else { throw AppError.message("B did not remove selected clips") }
        navigation.select(picks[0], extending: false)
        navigation.select(picks[0], extending: true)
        guard navigation.selectedIDs.isEmpty, navigation.focused == nil else { throw AppError.message("Deselecting final item left a selection") }
        print("MULTISELECT additive toggle, selection-only left/right preview, ignored up/down and bulk B shortcut passed")
        guard navigation.itemsForDrag(picks[0], in: picks).map(\.id) == [picks[0].id] else { throw AppError.message("Unselected drag did not select item") }
        navigation.select(picks[2], extending: true)
        guard navigation.itemsForDrag(picks[0], in: picks).map(\.id) == [picks[0].id, picks[2].id] else { throw AppError.message("Selected drag lost multi-selection") }
        guard navigation.itemsForDrag(picks[1], in: picks).map(\.id) == [picks[1].id], navigation.selectedIDs == [picks[1].id] else { throw AppError.message("New-item drag did not replace selection") }
        let boundedCache = FrameCache()
        for size in 64..<330 { _ = await boundedCache.image(picks[0].match.frame, maxPixelSize: size) }
        guard await boundedCache.retainedCount <= 256, await boundedCache.retainedCost <= 96 * 1024 * 1024 else { throw AppError.message("Thumbnail cache exceeded limits") }
        print("GRID DRAG selection replacement/multiple originals and bounded thumbnail cache passed")
        // Exercise actual AppKit card rectangles and mouse-down routing, not just
        // the selection helper: A selected, press/drag B, even after a stale A press.
        let dragRouter = MediaDragRouter()
        let dragA = library.items.first { $0.kind == "image" }!
        let dragB = library.items.first { $0.kind == "video" }!
        let dragMedia = [dragA, dragB]
        let dragWindow = NSWindow(contentRect: NSRect(x: 50, y: 50, width: 500, height: 240), styleMask: [.titled], backing: .buffered, defer: false)
        let dragRoot = NSView(frame: NSRect(x: 0, y: 0, width: 500, height: 240))
        let cardA = MediaDragSurface.DragView(frame: NSRect(x: 10, y: 10, width: 200, height: 200))
        let cardB = MediaDragSurface.DragView(frame: NSRect(x: 250, y: 10, width: 200, height: 200))
        cardA.items = { navigation.itemsForDrag(dragA, in: dragMedia) }
        cardB.items = { navigation.itemsForDrag(dragB, in: dragMedia) }
        dragRoot.addSubview(cardA); dragRoot.addSubview(cardB)
        dragWindow.contentView = dragRoot; dragWindow.makeKeyAndOrderFront(nil)
        dragRouter.register(cardA); dragRouter.register(cardB)
        func dragEvent(_ type: NSEvent.EventType, _ x: CGFloat) -> NSEvent {
            NSEvent.mouseEvent(with: type, location: NSPoint(x: x, y: 80), modifierFlags: [], timestamp: 0,
                               windowNumber: dragWindow.windowNumber, context: nil, eventNumber: 1, clickCount: 1, pressure: 1)!
        }
        navigation.select(dragA, extending: false)
        dragRouter.mouseDown(dragEvent(.leftMouseDown, 100))
        dragRouter.mouseDown(dragEvent(.leftMouseDown, 330))
        // A SwiftUI update must not replace the provider captured on mouse-down.
        cardB.items = { [dragA] }
        guard let routed = dragRouter.dragItems(dragEvent(.leftMouseDragged, 350)), routed.0 === cardB,
              routed.1.map(\.url) == [dragB.url], navigation.selectedIDs == [dragB.id],
              dragRouter.dragItems(dragEvent(.leftMouseDragged, 370)) == nil else {
            throw AppError.message("Mouse-down on B dragged stale selection A")
        }
        dragWindow.orderOut(nil); window.makeKeyAndOrderFront(nil)
        print("GRID DRAG AppKit mouse routing: stale A press, B press/drag, frozen provider, single session passed")
        let september = try DateSearch.parse("goats in September 2025")
        let leap = try DateSearch.parse("in February 2024")
        let exact = try DateSearch.parse("goats from 2026-01-01 to 2026-01-31")
        guard september.text == "goats", september.range == CaptureRange(from: "2025-09-01", through: "2025-09-30"),
              leap.text.isEmpty, leap.range?.through == "2024-02-29",
              exact.range?.contains("20260131235959") == true, exact.range?.contains("20260201000000") == false,
              exact.range?.contains(nil) == false,
              try DateSearch.parse("goats in September").range?.from == "\(Calendar.current.component(.year, from: Date()))-09-01" else {
            throw AppError.message("Natural-language date parsing or inclusive boundaries failed")
        }
        do {
            _ = try DateSearch.parse("from 2026-02-30 to 2026-03-01")
            throw AppError.message("Invalid date was accepted")
        } catch let error as AppError {
            if error.localizedDescription == "Invalid date was accepted" { throw error }
        }
        if let date = library.catalogMedia.compactMap({ $0.metadata?.sortDate }).first(where: { $0.count >= 8 }) {
            let day = "\(date.prefix(4))-\(date.dropFirst(4).prefix(2))-\(date.dropFirst(6).prefix(2))"
            library.query = "goats on \(day)"
            await library.search()?.value
            let count = library.catalogMedia.filter { library.matchesDate($0) }.count
            guard library.items.count == min(60, count), library.items.allSatisfy({ library.matchesDate($0) }) else { throw AppError.message("Date range was not applied before search limit") }
            library.query = "on \(day)"
            await library.search()?.value
            guard library.items.count == count, !library.showingMatches else { throw AppError.message("Date-only search failed") }
            library.query = ""; await library.search()?.value
        }
        print("DATES month/year, leap year, inclusive range, unknown dates, invalid dates and pre-limit search filtering passed")
        guard MediaFormat.classify(width: 1920, height: 1080, orientation: 6) == .vertical,
              MediaFormat.classify(width: 1080, height: 1920) == .vertical,
              MediaFormat.classify(width: 1080, height: 1080) == .square,
              MediaFormat.classify(width: 0, height: 1080) == .unknown,
              !library.formats.values.contains(.unknown) else { throw AppError.message("Display shape classification failed") }
        print("SHAPES \(Dictionary(grouping: library.formats.values, by: { $0.rawValue }).mapValues(\.count))")
        for format in [MediaFormat.vertical, .horizontal, .square] {
            library.format = format
            library.query = "goats"
            // Let the view's automatic filter-change search settle before awaiting this query.
            try await Task.sleep(for: .milliseconds(100))
            await library.search()?.value
            let available = library.formats.values.filter { $0 == format }.count
            guard library.items.count == min(60, available), library.items.allSatisfy({ library.matchesFormat($0) }) else {
                throw AppError.message("Shape filter failed for \(format): \(library.items.count) of \(available)")
            }
            try await Task.sleep(for: .milliseconds(200))
            try snapshot("shape-\(format.rawValue).png")
        }
        window.setContentSize(NSSize(width: 850, height: 650))
        try await Task.sleep(for: .milliseconds(200))
        try snapshot("compact-filters.png")
        window.setContentSize(NSSize(width: 1200, height: 800))
        library.format = .all
        if let device = library.devices.first {
            library.deviceFilter = device
            try await Task.sleep(for: .milliseconds(100))
            await library.search()?.value
            let count = library.catalogMedia.filter { $0.device == device }.count
            guard library.items.count == min(60, count), library.items.allSatisfy({ $0.device == device }) else {
                throw AppError.message("Device filter did not constrain search candidates")
            }
            try snapshot("device-filter.png")
            library.deviceFilter = "All devices"
            print("DEVICE filter passed: \(device), \(count) available originals")
        }
        try await Task.sleep(for: .milliseconds(100))
        library.query = "bringing food to goats"
        await library.search()?.value
        guard library.items.count == 60, library.showingMatches else { throw AppError.message("UI search failed") }
        try await Task.sleep(for: .seconds(1))
        try snapshot("search.png")
        if library.speechAvailable {
            library.query = "Dunăre"
            await library.search()?.value
            guard library.items.count == 60, library.items.contains(where: { $0.match.evidence != nil }),
                  Set(library.items.map(\.path)).count == library.items.count else {
                throw AppError.message("Combined search lost spoken matches or duplicated files")
            }
            try await Task.sleep(for: .milliseconds(300))
            try snapshot("both.png")
            print("BOTH combined search includes speech evidence and distinct files")
            library.mode = "speech"
            try await Task.sleep(for: .milliseconds(200))
            library.query = "Dunăre"
            await library.search()?.value
            guard !library.items.isEmpty, library.items.allSatisfy({ $0.match.evidence != nil }) else {
                throw AppError.message("Spoken-word search failed")
            }
            try await Task.sleep(for: .seconds(1))
            try snapshot("speech.png")
            print("SPEECH_MATCHES \(library.items.count)")
        }
        library.query = ""
        await library.search()?.value
        guard library.items.count == 498, !library.showingMatches else { throw AppError.message("Clear search failed") }
        await library.worker.stop()
        var cameraConnections = CameraConnections()
        guard Indexing.activeEditing(bundleID: "com.lemon.lvoverseas", idleSeconds: 3),
              !Indexing.activeEditing(bundleID: "com.lemon.lvoverseas", idleSeconds: 60),
              !Indexing.activeEditing(bundleID: "com.lemon.lvoverseas", idleSeconds: 600),
              !Indexing.activeEditing(bundleID: "local.mami.prototype", idleSeconds: 0),
              !Indexing.activeEditing(bundleID: nil, idleSeconds: 0) else {
            throw AppError.message("CapCut activity must require foreground and recent input")
        }
        print("EDITOR foreground/recent input yields; idle, background and Mami activity do not")
        let cameraFixture = URL(fileURLWithPath: "/Volumes/DJI-Test")
        guard cameraConnections.next([cameraFixture], enabled: false, busy: false) == nil,
              cameraConnections.next([cameraFixture], enabled: true, busy: true) == nil,
              cameraConnections.next([cameraFixture], enabled: true, busy: false) == cameraFixture,
              cameraConnections.next([cameraFixture], enabled: true, busy: false) == nil else {
            throw AppError.message("Camera enable/busy/once-per-connection scheduling failed")
        }
        cameraConnections.disconnected(cameraFixture)
        guard cameraConnections.next([cameraFixture], enabled: true, busy: false) == cameraFixture else {
            throw AppError.message("Camera reconnect retry failed")
        }
        cameraConnections.retry()
        guard cameraConnections.next([cameraFixture], enabled: true, busy: false) == cameraFixture else {
            throw AppError.message("Camera explicit retry failed")
        }
        print("CAMERA automatic enable, busy deferral, once-per-connection, reconnect and explicit retry passed")
        if let source = ProcessInfo.processInfo.environment["MAMI_IMPORT_TEST_SOURCE"],
           ProcessInfo.processInfo.environment["MAMI_IMPORT_TEST_DESTINATION"] != nil {
            let importer = Importing.shared
            importer.source = URL(fileURLWithPath: source)
            importer.device = "Import-Fixture"
            let importHost = NSHostingView(rootView: ImportSheet())
            importHost.appearance = NSAppearance(named: .darkAqua)
            window.contentView = importHost
            importer.start()
            let deadline = Date().addingTimeInterval(90)
            while importer.running && Date() < deadline { try await Task.sleep(for: .milliseconds(50)) }
            guard !importer.running, importer.error == nil, importer.progress?.phase == "Import complete",
                  importer.progress?.copied == 1, importer.progress?.duplicates == 1,
                  importer.progress?.failed == 0 else { throw AppError.message("Native import failed: \(importer.error ?? importer.progress?.phase ?? "No progress")") }
            try await Task.sleep(for: .milliseconds(300))
            importHost.layoutSubtreeIfNeeded()
            if let view = window.contentView, let bitmap = view.bitmapImageRepForCachingDisplay(in: view.bounds) {
                view.cacheDisplay(in: view.bounds, to: bitmap)
                try bitmap.representation(using: .png, properties: [:])?.write(to: directory.appendingPathComponent("import-complete.png"), options: .withoutOverwriting)
            }
            print("IMPORT native controller verified one new copy and one existing catalog duplicate")
        }
        window.orderOut(nil)
        if ProcessInfo.processInfo.environment["MAMI_PHOTOS_TRANSFER_TEST"] == "1" {
            let incoming = directory.appendingPathComponent("photos-fixture/incoming")
            let receiptFolder = incoming.appendingPathComponent(".receipts")
            let destination = directory.appendingPathComponent("photos-fixture/originals")
            try FileManager.default.createDirectory(at: receiptFolder, withIntermediateDirectories: true)
            let original = library.items.first { $0.kind == "image" }!
            let staged = incoming.appendingPathComponent(original.url.lastPathComponent)
            try FileManager.default.copyItem(at: original.url, to: staged)
            let digest = try await Task.detached { try PhotosExporter.hash(staged) }.value
            let size = (try FileManager.default.attributesOfItem(atPath: staged.path)[.size] as! NSNumber).int64Value
            let receiptURL = receiptFolder.appendingPathComponent("native-" + UUID().uuidString + ".json")
            try JSONEncoder().encode(PhotosExporter.Receipt(file: staged.lastPathComponent, digest: digest, size: size, imported: false, captureDate: Date()))
                .write(to: receiptURL)
            _ = try Catalog.standard.photosHistory()
            try await Importing.shared.importPhotosFolder(incoming, destination: destination, receipts: [receiptURL])
            let saved = try JSONSerialization.jsonObject(with: Data(contentsOf: receiptURL)) as! [String: Any]
            guard let path = saved["destination"] as? String,
                  try PhotosExporter.hash(URL(fileURLWithPath: path)) == digest,
                  !FileManager.default.fileExists(atPath: staged.path),
                  try Catalog.standard.photosHistory().contains(receiptURL.deletingPathExtension().lastPathComponent) else {
                throw AppError.message("Native Photos batch verification/history/cleanup failed")
            }
            print("PHOTOS native controller copied real media, verified history, removed staging and preserved final bytes")
        }
        let video = library.items.first { $0.kind == "video" }!
        let player = try await preparedPlayer(url: video.url, timestamp: 5)
        let transport = PlaybackTransport()
        transport.muted = true
        transport.attach(player)
        guard abs(transport.position - 5) < 0.2, transport.duration > 5 else {
            throw AppError.message("Initial transport position was not populated")
        }
        await transport.seek(to: 10, resume: false)?.value
        guard abs(transport.position - 10) < 0.2 else {
            throw AppError.message("Transport position failed to refresh after seeking")
        }
        let playbackHost = NSHostingView(rootView: VStack {
            VideoSurface(player: transport.player)
            TransportBar(transport: transport)
        }.padding().preferredColorScheme(.dark))
        window.contentView = playbackHost
        window.orderFrontRegardless()
        try await Task.sleep(for: .milliseconds(300))
        func key(_ characters: String, code: UInt16) throws {
            guard let event = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: [], timestamp: 0,
                                              windowNumber: window.windowNumber, context: nil, characters: characters,
                                              charactersIgnoringModifiers: characters, isARepeat: false, keyCode: code),
                  window.performKeyEquivalent(with: event) else { throw AppError.message("Transport shortcut was not handled: \(code)") }
        }
        try key("\u{f702}", code: 123)
        try await Task.sleep(for: .milliseconds(400))
        guard abs(transport.position - 5) < 0.2 else { throw AppError.message("Left-arrow seek failed") }
        try key("\u{f703}", code: 124)
        try await Task.sleep(for: .milliseconds(400))
        guard abs(transport.position - 10) < 0.2 else { throw AppError.message("Right-arrow seek failed") }
        try key("m", code: 46)
        guard !transport.muted else { throw AppError.message("Mute shortcut failed") }
        try key("m", code: 46)
        guard transport.muted else { throw AppError.message("Mute shortcut did not toggle back") }
        print("TRANSPORT left/right seek and mute keyboard shortcuts passed")
        if CommandLine.arguments.contains("--verify-playing") {
            try key(" ", code: 49)
            try await Task.sleep(for: .seconds(2))
            guard transport.position > 10.5, abs(transport.position - player.currentTime().seconds) < 0.4 else {
                throw AppError.message("Transport did not follow playback without hover: \(transport.position), player \(player.currentTime().seconds)")
            }
            try key(" ", code: 49)
            let paused = transport.position
            try await Task.sleep(for: .milliseconds(500))
            guard abs(transport.position - paused) < 0.25, !transport.playing else {
                throw AppError.message("Transport did not pause")
            }
            transport.scrub(true)
            transport.position = 5
            transport.scrub(false)
            try await Task.sleep(for: .milliseconds(700))
            guard abs(transport.position - 5) < 0.2, !transport.playing else {
                throw AppError.message("Paused scrubbing did not remain paused")
            }
            transport.toggle()
            try await Task.sleep(for: .milliseconds(300))
            transport.scrub(true)
            transport.position = 10
            transport.scrub(false)
            try await Task.sleep(for: .seconds(2))
            guard transport.position > 10.5, transport.playing else {
                throw AppError.message("Playing scrub did not resume")
            }
            transport.toggle()
            print("TRANSPORT continuous updates, pause, paused scrub and playing scrub passed")
        }
        playbackHost.layoutSubtreeIfNeeded()
        if let bitmap = playbackHost.bitmapImageRepForCachingDisplay(in: playbackHost.bounds) {
            playbackHost.cacheDisplay(in: playbackHost.bounds, to: bitmap)
            try bitmap.representation(using: .png, properties: [:])?.write(to: directory.appendingPathComponent("transport.png"), options: .withoutOverwriting)
        }
        transport.stop()
        window.orderOut(nil)
        print("TRANSPORT initial position and seek refresh passed without hover")
        library.query = ""; library.format = .all; library.deviceFilter = "All devices"; library.dateEnabled = false
        await library.search()?.value
        library.gridLocked = true
        let before = library.items.map(\.id)
        let original = library.catalogMedia.first { $0.kind == "image" }!
        let arrivalURL = directory.appendingPathComponent("grid-arrival." + original.url.pathExtension)
        try FileManager.default.copyItem(at: original.url, to: arrivalURL)
        let arrival = Media(path: "grid-arrival", kind: original.kind, url: arrivalURL, frames: original.frames,
                            match: original.match, metadata: original.metadata, assetID: "fixture:grid-arrival")
        try Catalog.standard.synchronize([arrival])
        await library.refreshCatalog()
        guard library.items.map(\.id) == before, library.pendingMediaCount == 1 else { throw AppError.message("Locked grid changed during catalog refresh") }
        await library.search()?.value
        guard library.items.map(\.id) == before else { throw AppError.message("Search escaped locked catalog snapshot") }
        library.refreshGrid()
        guard library.gridLocked, library.pendingMediaCount == 0, library.items.contains(where: { $0.id == arrival.id }) else {
            throw AppError.message("Explicit refresh did not include new media while retaining lock")
        }
        library.gridLocked = false
        print("GRID LOCK stable snapshot, one pending arrival, explicit refresh and unlock passed")
        print("UI TEST PASSED: \(directory.path)")
    }

    @MainActor static func selfTest() async throws {
        let config = try Configuration.load()
        let media = try Library.readMedia(config.index)
        guard media.count == 498 else { throw AppError.message("Expected 498 files, got \(media.count)") }
        let frames = media.filter { $0.kind == "video" }.prefix(12).flatMap { Array($0.frames.prefix(12)) }
        var cold: [Double] = [], warm: [Double] = []
        for frame in frames {
            let start = CFAbsoluteTimeGetCurrent()
            guard await FrameCache.shared.image(frame.frame, crop: frame.crop) != nil else { throw AppError.message("Missing cached frame \(frame.frame)") }
            cold.append((CFAbsoluteTimeGetCurrent() - start) * 1000)
        }
        for frame in frames {
            let start = CFAbsoluteTimeGetCurrent()
            _ = await FrameCache.shared.image(frame.frame, crop: frame.crop)
            warm.append((CFAbsoluteTimeGetCurrent() - start) * 1000)
        }
        func percentile(_ values: [Double]) -> Double { values.sorted()[min(values.count - 1, Int(Double(values.count) * 0.95))] }
        print("FILES \(media.count) FRAMES \(media.reduce(0) { $0 + $1.frames.count })")
        print("THUMBNAIL_MS cold_p95=\(percentile(cold)) warm_p95=\(percentile(warm)) samples=\(frames.count)")
        for item in media.filter({ $0.kind == "video" }).prefix(3) {
            let asset = AVURLAsset(url: item.url)
            let duration = try await asset.load(.duration).seconds
            let generator = AVAssetImageGenerator(asset: asset)
            generator.appliesPreferredTrackTransform = true
            generator.maximumSize = CGSize(width: 960, height: 540)
            generator.requestedTimeToleranceBefore = .zero
            generator.requestedTimeToleranceAfter = CMTime(seconds: 0.1, preferredTimescale: 600)
            let start = CFAbsoluteTimeGetCurrent()
            _ = try await generator.image(at: CMTime(seconds: duration / 2, preferredTimescale: 600))
            print("ORIGINAL_SEEK_MS \((CFAbsoluteTimeGetCurrent() - start) * 1000) \(item.path)")
        }
        if let video = media.first(where: { $0.kind == "video" }) {
            let start = CFAbsoluteTimeGetCurrent()
            let player = try await preparedPlayer(url: video.url, timestamp: 5)
            player.isMuted = true
            let videoView = AVPlayerView(frame: NSRect(x: 0, y: 0, width: 640, height: 360))
            videoView.player = player
            let playbackWindow = NSWindow(contentRect: videoView.frame, styleMask: [.titled], backing: .buffered, defer: false)
            playbackWindow.contentView = videoView
            playbackWindow.orderFrontRegardless()
            print("PLAYER_SEEK_MS \((CFAbsoluteTimeGetCurrent() - start) * 1000)")
            player.play()
            try await Task.sleep(for: .seconds(2))
            let position = player.currentTime().seconds
            print("PLAYER_STATE position=\(position) rate=\(player.rate) control=\(player.timeControlStatus.rawValue) reason=\(String(describing: player.reasonForWaitingToPlay)) error=\(String(describing: player.error))")
            player.pause()
            player.replaceCurrentItem(with: nil)
            playbackWindow.orderOut(nil)
            guard position > 5.2 else { throw AppError.message("AVPlayer did not advance after seek") }
            print("PLAYER_ADVANCED_TO \(position)")
        }
        let worker = SearchWorker()
        do {
            try await worker.start(config)
            for query in ["bringing food to goats", "milking goats", "picking plums"] {
                let start = CFAbsoluteTimeGetCurrent()
                let reply = try await worker.search(query)
                guard let hits = reply.hits, hits.count == 60, Set(hits.map(\.path)).count == hits.count,
                      hits.allSatisfy({ hit in media.contains { $0.path == hit.path } }) else {
                    throw AppError.message("Search returned invalid results")
                }
                print("SEARCH_MS \((CFAbsoluteTimeGetCurrent() - start) * 1000) \(query)")
            }
            await worker.stop()
        } catch { await worker.stop(); throw error }
        print("SELF-TEST PASSED")
    }
}
