import SwiftUI
import AVKit
import ImageIO

// Explicit alias selects the property wrapper on SDKs that also export a State macro.
typealias ViewState<Value> = SwiftUI.State<Value>

final class FrameCache: @unchecked Sendable {
    static let shared = FrameCache()
    private let images = NSCache<NSString, NSImage>()
    private let queue = DispatchQueue(label: "mami.frames", qos: .userInitiated, attributes: .concurrent)
    init() { images.totalCostLimit = 192 * 1024 * 1024 }

    func image(_ path: String, maxPixelSize: Int = 640) async -> NSImage? {
        let key = "\(maxPixelSize):\(path)"
        if let cached = images.object(forKey: key as NSString) { return cached }
        return await withCheckedContinuation { continuation in
            queue.async {
                guard let source = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil),
                      let cg = CGImageSourceCreateThumbnailAtIndex(source, 0, [
                        kCGImageSourceCreateThumbnailFromImageAlways: true,
                        kCGImageSourceThumbnailMaxPixelSize: maxPixelSize,
                        kCGImageSourceCreateThumbnailWithTransform: true,
                        kCGImageSourceShouldCacheImmediately: true
                      ] as CFDictionary) else { continuation.resume(returning: nil); return }
                let image = NSImage(cgImage: cg, size: NSSize(width: cg.width, height: cg.height))
                self.images.setObject(image, forKey: key as NSString, cost: cg.bytesPerRow * cg.height)
                continuation.resume(returning: image)
            }
        }
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
    let nearby: Bool
    @ObservedObject var annotations: Annotations
    let open: (Media, Double?) -> Void
    @ViewState private var hovered: Int?
    @ViewState private var image: NSImage?
    @ViewState private var unavailable = false
    private var annotation: Annotation { annotations.value(for: media) }
    private var subtitle: String {
        let labels = ([annotation.place].filter { !$0.isEmpty } + annotation.tags.map { "#" + $0 })
        if !labels.isEmpty { return labels.joined(separator: " · ") }
        return media.match.evidence.map { "“\($0)”" } ?? media.metadata?.subtitle ?? media.kind.capitalized
    }
    private var scrubFrames: [Sample] {
        guard nearby, let timestamp = media.match.timestamp else { return media.frames }
        return media.frames.filter { abs(($0.timestamp ?? 0) - timestamp) <= 8 }
    }
    private var sample: Sample {
        if let hovered, scrubFrames.indices.contains(hovered) { return scrubFrames[hovered] }
        return media.match
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            GeometryReader { geometry in
                ZStack(alignment: .bottomLeading) {
                    Rectangle().fill(Color.black.opacity(0.65))
                    if let image { Image(nsImage: image).resizable().scaledToFit().frame(maxWidth: .infinity, maxHeight: .infinity) }
                    else { Image(systemName: unavailable ? "exclamationmark.triangle" : "photo").frame(maxWidth: .infinity, maxHeight: .infinity).foregroundStyle(.secondary) }
                    Text(timeLabel(sample.timestamp)).font(.caption.monospacedDigit()).padding(5).background(.black.opacity(0.7)).padding(5)
                    if let hovered {
                        Rectangle().fill(Color.accentColor).frame(width: geometry.size.width * CGFloat(hovered + 1) / CGFloat(max(1, scrubFrames.count)), height: 3)
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
                .contentShape(Rectangle())
                .onContinuousHover { phase in
                    switch phase {
                    case .active(let point):
                        guard media.kind == "video" else { return }
                        let fraction = point.x / max(1, geometry.size.width)
                        hovered = min(scrubFrames.count - 1, max(0, Int(fraction * CGFloat(scrubFrames.count))))
                    case .ended: hovered = nil
                    }
                }
                .onTapGesture { open(media, sample.timestamp) }
            }.frame(height: 175).clipShape(RoundedRectangle(cornerRadius: 8))
            HStack(spacing: 6) {
                Text(media.metadata?.date ?? "Capture date unavailable").font(.system(size: 12, weight: .medium)).lineLimit(1)
                Spacer(minLength: 0)
                Text(media.kind == "image" ? "Photo" : media.metadata?.duration.map { timeLabel($0) } ?? "Video").font(.caption.monospacedDigit()).foregroundStyle(.secondary)
            }
            Text(subtitle)
                .font(.caption).foregroundStyle(.secondary).lineLimit(1)
                .help(([media.title, media.metadata?.subtitle ?? "", media.match.evidence ?? ""] + (media.metadata?.tags ?? []) + (media.metadata?.technical ?? [])).filter { !$0.isEmpty }.joined(separator: "\n"))
        }
        .padding(9)
        .background(Color(red: 0.14, green: 0.15, blue: 0.17), in: RoundedRectangle(cornerRadius: 10))
        .task(id: sample.frame) {
            let path = sample.frame
            let decoded = await FrameCache.shared.image(path)
            guard !Task.isCancelled else { return }
            image = decoded
            unavailable = decoded == nil
            if let position = scrubFrames.firstIndex(where: { $0.frame == path }) {
                for neighbor in [position + 1, position - 1, position + 2, position - 2] {
                    guard !Task.isCancelled else { return }
                    if scrubFrames.indices.contains(neighbor) {
                        _ = await FrameCache.shared.image(scrubFrames[neighbor].frame)
                    }
                }
            }
        }
        .onChange(of: media.match.frame) { _, _ in hovered = nil }
        .onChange(of: nearby) { _, _ in hovered = nil }
        .contextMenu {
            Button("Play from match") { open(media, media.match.timestamp) }
            Button("Reveal in Finder") { NSWorkspace.shared.activateFileViewerSelecting([media.url]) }
        }
        .help("Move across the thumbnail to scrub. Click to open at the displayed moment.")
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
    var previous: (() -> Void)? = nil
    var next: (() -> Void)? = nil
    @StateObject private var transport = PlaybackTransport()
    @ViewState private var photo: NSImage?
    @ViewState private var failure: String?
    @ViewState private var showInfo = false
    @ViewState private var editingMetadata = false
    @ViewState private var tags = ""
    @ViewState private var place = ""
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(spacing: 12) {
            HStack {
                VStack(alignment: .leading) {
                    Text(selection.media.metadata?.date ?? selection.media.title).font(.headline)
                    Text(selection.media.metadata?.subtitle ?? selection.media.path).font(.caption).foregroundStyle(.secondary).help(selection.media.path)
                }
                Spacer()
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
                Button { previous?() } label: { Image(systemName: "chevron.left") }
                    .disabled(previous == nil).keyboardShortcut(.leftArrow, modifiers: .command).help("Previous result (⌘←)")
                Button { next?() } label: { Image(systemName: "chevron.right") }
                    .disabled(next == nil).keyboardShortcut(.rightArrow, modifiers: .command).help("Next result (⌘→)")
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
                Button("Reveal in Finder") { NSWorkspace.shared.activateFileViewerSelecting([selection.media.url]) }
                Button("Done") { dismiss() }.keyboardShortcut(.cancelAction)
            }
            if let failure { ContentUnavailableView("Media unavailable", systemImage: "externaldrive.badge.exclamationmark", description: Text(failure)) }
            else if selection.media.kind == "video" {
                ZStack {
                    VideoSurface(player: transport.player)
                    if transport.player == nil { ProgressView("Opening video…") }
                }
                TransportBar(transport: transport, shortcutsEnabled: !editingMetadata && !showInfo)
            }
            else if let photo { Image(nsImage: photo).resizable().scaledToFit() }
            else { ProgressView() }
        }
        .padding().frame(minWidth: 850, minHeight: 580)
        .task {
            guard FileManager.default.isReadableFile(atPath: selection.media.url.path) else {
                failure = "The original file is offline or inaccessible. Cached previews remain available."
                return
            }
            if selection.media.kind == "video" {
                do {
                    let p = try await preparedPlayer(url: selection.media.url, timestamp: selection.timestamp ?? 0)
                    guard !Task.isCancelled else { p.pause(); return }
                    transport.attach(p)
                    transport.toggle()
                } catch { if !Task.isCancelled { failure = error.localizedDescription } }
            } else {
                photo = await FrameCache.shared.image(selection.media.url.path, maxPixelSize: 2048)
                if photo == nil { failure = "Could not decode this photo." }
            }
        }
        .onAppear { Indexing.shared.beginPreview(selection.id) }
        .onDisappear { transport.stop(); Indexing.shared.endPreview(selection.id) }
    }
}

struct LibraryView: View {
    @StateObject private var library: Library
    @ViewState private var selection: Selection?
    @ViewState private var nearby = true
    @ViewState private var kind = "all"
    @ViewState private var sort = "default"
    @ViewState private var favoritesOnly = false
    @ViewState private var labelFilter = ""
    @ViewState private var showImport = false
    @ObservedObject private var importing = Importing.shared
    @StateObject private var annotations = Annotations()
    @ObservedObject private var backups = CatalogBackups.shared
    @ObservedObject private var indexing = Indexing.shared
    @FocusState private var searchFocused: Bool
    private var visibleItems: [Media] {
        let filtered = library.items.filter {
            let value = annotations.value(for: $0)
            let labels = ([value.place] + value.tags).joined(separator: " ")
            return (kind == "all" || $0.kind == kind) && (!favoritesOnly || value.favorite)
                && library.matchesFormat($0)
                && (labelFilter.isEmpty || labels.localizedStandardContains(labelFilter))
        }
        if sort == "default" && library.showingMatches { return filtered }
        return filtered.sorted {
            let a = $0.metadata?.sortDate ?? "", b = $1.metadata?.sortDate ?? ""
            if a == b { return $0.path < $1.path }
            if a.isEmpty { return false }; if b.isEmpty { return true }
            return sort == "oldest" ? a < b : a > b
        }
    }
    private var hasFilters: Bool { kind != "all" || library.format != .all || favoritesOnly || !labelFilter.isEmpty }
    private func clearFilters() {
        kind = "all"; library.format = .all; favoritesOnly = false; labelFilter = ""
    }
    private func adjacent(to selection: Selection, offset: Int) -> Media? {
        let items = visibleItems
        guard let index = items.firstIndex(where: { $0.id == selection.media.id }), items.indices.contains(index + offset) else { return nil }
        return items[index + offset]
    }
    @MainActor init(library: Library? = nil) { _library = StateObject(wrappedValue: library ?? Library()) }
    var body: some View {
        VStack(spacing: 0) {
            VStack(spacing: 14) {
                HStack {
                    Text("Mami").font(.system(size: 25, weight: .semibold, design: .rounded))
                    Text("Your moments, within reach").font(.callout).foregroundStyle(.secondary)
                    Spacer()
                    Button(importing.running ? "Import progress…" : "Import media…") { showImport = true }
                    Label("On this Mac", systemImage: "desktopcomputer").font(.caption).foregroundStyle(.secondary)
                }
                HStack(spacing: 14) {
                    Image(systemName: "magnifyingglass").font(.title2).foregroundStyle(.secondary)
                    TextField(library.mode == "speech" ? "Find words spoken in a video…" : "What are you looking for?", text: $library.query)
                        .font(.system(size: 21)).textFieldStyle(.plain).onSubmit { library.search() }
                        .focused($searchFocused)
                        .accessibilityLabel("Search your media")
                    if !library.query.isEmpty {
                        Button { library.query = ""; library.search() } label: { Image(systemName: "xmark.circle.fill") }
                            .buttonStyle(.plain).foregroundStyle(.secondary).help("Clear search")
                    }
                    if library.searching { ProgressView().controlSize(.small) }
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
                Text(library.mode == "speech" ? "Find Romanian words spoken in videos · Accents are optional"
                     : library.mode == "both" ? "Search visuals and Romanian speech together · Matches may be approximate"
                     : "Try “bringing food to goats”, “picking plums” or “grilling by a river”")
                    .font(.caption).foregroundStyle(.secondary)
            }.padding(.horizontal, 24).padding(.top, 20).padding(.bottom, 18)
            HStack {
                Text(hasFilters ? "\(visibleItems.count) shown · \(library.status)" : library.status)
                Spacer()
                Text("Hover to scrub · Click to open").foregroundStyle(.secondary)
            }.font(.caption).padding(.horizontal, 14).padding(.bottom, 10)
            HStack {
                Picker("Show", selection: $kind) {
                    Text("All media").tag("all")
                    Text("Videos").tag("video")
                    Text("Photos").tag("image")
                }.pickerStyle(.segmented).frame(width: 260)
                Picker("Shape", selection: $library.format) {
                    ForEach(MediaFormat.allCases) { format in Text(format.label).tag(format) }
                }.frame(width: 190)
                    .help("Vertical: Reels, TikTok & Shorts. Horizontal: YouTube & widescreen. Square: social feeds. Filters original shape; does not crop or resize.")
                    .onChange(of: library.format) { _, _ in library.search() }
                Picker("Sort", selection: $sort) {
                    Text(library.showingMatches ? "Best match" : "Newest first").tag("default")
                    Text("Capture date: newest").tag("newest")
                    Text("Oldest first").tag("oldest")
                }.labelsHidden().frame(width: 135)
                Toggle(isOn: $favoritesOnly) { Image(systemName: "heart.fill") }.toggleStyle(.button).help("Show favorites only").accessibilityLabel("Favorites only")
                Spacer()
            }.padding(.horizontal, 24).padding(.bottom, 10)
            if library.format != .all {
                HStack {
                    Text(library.format.guidance).font(.caption).foregroundStyle(.secondary)
                    Spacer()
                }.padding(.horizontal, 24).padding(.bottom, 8)
            }
            HStack {
                Image(systemName: "tag").foregroundStyle(.secondary)
                TextField("Filter your tags or places", text: $labelFilter).textFieldStyle(.roundedBorder).frame(maxWidth: 250)
                if !labelFilter.isEmpty {
                    Button { labelFilter = "" } label: { Image(systemName: "xmark.circle") }.buttonStyle(.plain)
                }
                if favoritesOnly { Text("Favorites").font(.caption).foregroundStyle(.pink) }
                if hasFilters { Button("Clear filters") { clearFilters() }.font(.caption) }
                Spacer()
                if library.showingMatches {
                    Toggle("Scrub ±8 seconds around match", isOn: $nearby).toggleStyle(.switch).controlSize(.small)
                }
            }.padding(.horizontal, 14).padding(.bottom, 10)
            if let error = library.error { Text(error).foregroundStyle(.red).padding() }
            if let error = annotations.error { Text(error).foregroundStyle(.red).font(.caption).padding() }
            HStack {
                Label(backups.status, systemImage: "externaldrive.badge.checkmark").font(.caption).foregroundStyle(.secondary)
                Spacer()
                Button("Show backups") { NSWorkspace.shared.open(Catalog.standard.backups) }.font(.caption)
            }.padding(.horizontal, 14).padding(.bottom, 8)
            if let error = backups.error { Text(error).foregroundStyle(.red).font(.caption).padding() }
            IndexingBar()
            Divider()
            ScrollView {
                if visibleItems.isEmpty, library.ready, !library.searching {
                    ContentUnavailableView {
                        Label(hasFilters ? "No media fits these filters" : "No search results", systemImage: "magnifyingglass")
                    } description: {
                        Text(hasFilters ? "Try another shape or clear the filters to see more of your library." : "Try fewer words. Speech search finds Romanian words within a single spoken passage; accents are optional.")
                    } actions: {
                        if hasFilters { Button("Clear filters") { clearFilters() } }
                    }
                }
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 240, maximum: 360), spacing: 14)], spacing: 14) {
                    ForEach(visibleItems) { media in
                        MediaCard(media: media, nearby: nearby && library.showingMatches, annotations: annotations) { item, timestamp in selection = Selection(media: item, timestamp: timestamp) }
                    }
                }.padding(14)
            }
        }
        .frame(minWidth: 850, minHeight: 600)
        .background(Color(red: 0.08, green: 0.09, blue: 0.11))
        .preferredColorScheme(.dark)
        .background(Button("Focus search") { searchFocused = true }.keyboardShortcut("f", modifiers: .command).hidden())
        .task { await library.load() }
        .task { await annotations.load() }
        .task {
            while !Task.isCancelled {
                do { try await Task.sleep(for: .seconds(60)) } catch { return }
                backups.schedule()
            }
        }
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.willTerminateNotification)) { _ in importing.shutdown(); indexing.stop(); backups.flush() }
        .onReceive(NSWorkspace.shared.notificationCenter.publisher(for: NSWorkspace.didLaunchApplicationNotification)) { _ in indexing.refreshEditors() }
        .onReceive(NSWorkspace.shared.notificationCenter.publisher(for: NSWorkspace.didTerminateApplicationNotification)) { _ in indexing.refreshEditors() }
        .task(id: indexing.catalogGeneration) {
            if indexing.catalogGeneration > 0 { await library.refreshCatalog() }
        }
        .sheet(item: $selection) { item in
            let before = adjacent(to: item, offset: -1)
            let after = adjacent(to: item, offset: 1)
            Playback(selection: item, annotations: annotations,
                     previous: before.map { media in { selection = Selection(media: media, timestamp: media.match.timestamp) } },
                     next: after.map { media in { selection = Selection(media: media, timestamp: media.match.timestamp) } })
                .id(item.id)
        }
        .sheet(isPresented: $showImport) { ImportSheet() }
    }
}

struct MamiApp: App {
    var body: some Scene {
        WindowGroup("Mami") { LibraryView() }
            .defaultSize(width: 1200, height: 800)
    }
}

@main enum EntryPoint {
    @MainActor static func main() {
        if let index = CommandLine.arguments.firstIndex(of: "--scan-ui-test"), CommandLine.arguments.indices.contains(index + 1) {
            NSApplication.shared.setActivationPolicy(.accessory)
            Task {
                do { try await scanUITest(URL(fileURLWithPath: CommandLine.arguments[index + 1])); exit(0) }
                catch { Indexing.shared.stop(); print("SCAN UI TEST FAILED: \(error)"); exit(1) }
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
        indexing.start()
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
        window.orderOut(nil)
        print("SCAN UI TEST PASSED: native progress, persisted pause, no writes while paused, resume and 498-file automatic scan; CapCut detected=\(indexing.capCutRunning)")
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
              try SQLDatabase(annotationTest.catalog.database, readOnly: true).scalar("SELECT count(*) FROM annotation_history") == "2" else {
            throw AppError.message("Annotation history was not preserved")
        }
        let host = NSHostingView(rootView: LibraryView(library: library))
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
            guard await FrameCache.shared.image(frame.frame) != nil else { throw AppError.message("Missing cached frame \(frame.frame)") }
            cold.append((CFAbsoluteTimeGetCurrent() - start) * 1000)
        }
        for frame in frames {
            let start = CFAbsoluteTimeGetCurrent()
            _ = await FrameCache.shared.image(frame.frame)
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
