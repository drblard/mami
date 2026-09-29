import AppKit
import SwiftUI

struct SelectedClip: Identifiable, Codable, Sendable, Equatable {
    let assetID: String
    let path: String
    let kind: String
    var url: URL
    var frame: String
    var crop: [Int]? = nil
    let timestamp: Double?
    var id: String { assetID }
    init(_ media: Media, sample: Sample? = nil) {
        let sample = sample ?? media.match
        assetID = media.assetID; path = media.path; kind = media.kind
        url = media.url; frame = sample.frame; timestamp = sample.timestamp
        crop = sample.crop
    }
    var media: Media {
        let sample = Sample(path: path, kind: kind, timestamp: timestamp, frame: frame, score: nil, evidence: nil, crop: crop)
        return Media(path: path, kind: kind, url: url, frames: [sample], match: sample, metadata: nil, assetID: assetID)
    }
}

@MainActor final class ClipSelection: ObservableObject {
    @Published private(set) var items: [SelectedClip] = []
    @Published private(set) var ready = false
    @Published var error: String?
    private let catalog: Catalog
    init(catalog: Catalog = .standard) { self.catalog = catalog }
    func contains(_ media: Media) -> Bool { items.contains { $0.assetID == media.assetID } }
    func load() async {
        guard !ready else { return }
        do {
            let catalog = catalog
            let loaded = try await Task.detached(priority: .utility) { try catalog.selectedClips() }.value
            items = loaded; ready = true
            let live = try await Task.detached(priority: .utility) { try catalog.media(forAssetIDs: loaded.map(\.assetID)) }.value
            reconnect(live)
        } catch { self.error = "Could not load selected clips: \(error.localizedDescription)" }
    }
    func reconnect(_ media: [Media]) {
        let byAsset = Dictionary(media.map { ($0.assetID, $0) }, uniquingKeysWith: { first, _ in first })
        let updated = items.map { item in
            guard let live = byAsset[item.assetID] else { return item }
            var value = item; value.url = live.url
            if !FileManager.default.fileExists(atPath: value.frame), let sample = live.frames.first { value.frame = sample.frame; value.crop = sample.crop }
            return value
        }
        // URLs and cached frames are derived. Reconnection should not create a
        // personal-data revision or backup unless the user edits the selection.
        items = updated
    }
    func save(_ updated: [SelectedClip]) {
        guard ready, updated != items else { return }
        do {
            try catalog.saveSelectedClips(updated)
            items = updated; error = nil
            if catalog.directory == Catalog.standard.directory { CatalogBackups.shared.schedule(urgent: true) }
        } catch { self.error = "Could not save selection: \(error.localizedDescription)" }
    }
    func toggle(_ media: Media, sample: Sample? = nil) {
        if contains(media) { remove(media.assetID) }
        else { save(items + [SelectedClip(media, sample: sample)]) }
    }
    func remove(_ assetID: String) { save(items.filter { $0.assetID != assetID }) }
    func toggle(_ media: [Media]) {
        guard !media.isEmpty else { return }
        let ids = Set(media.map(\.assetID))
        if media.allSatisfy({ contains($0) }) { save(items.filter { !ids.contains($0.assetID) }) }
        else { save(items + media.filter { !contains($0) }.map { SelectedClip($0) }) }
    }
    func clear() { save([]) }
    func move(_ id: String, by offset: Int) {
        guard let index = items.firstIndex(where: { $0.id == id }), items.indices.contains(index + offset) else { return }
        var next = items; next.swapAt(index, index + offset); save(next)
    }
}

/// A file per dragging item gives CapCut/Finder native multi-file drop data.
/// SwiftUI's single onDrag provider is not a multi-file drag session.
struct ClipDragHandle: NSViewRepresentable {
    let clips: [SelectedClip]
    let failure: (String) -> Void
    static func writers(_ clips: [SelectedClip]) throws -> [NSURL] {
        for clip in clips where !FileManager.default.isReadableFile(atPath: clip.url.path) {
            throw AppError.message("Original unavailable: \(clip.url.lastPathComponent). Reconnect it before dragging the selection.")
        }
        return clips.map { $0.url as NSURL }
    }
    func makeNSView(context: Context) -> DragView { DragView() }
    func updateNSView(_ view: DragView, context: Context) { view.clips = clips; view.failure = failure; view.needsDisplay = true }
    final class DragView: NSView, NSDraggingSource {
        var clips: [SelectedClip] = []
        var failure: (String) -> Void = { _ in }
        override init(frame frameRect: NSRect) {
            super.init(frame: frameRect)
            setAccessibilityElement(true)
            setAccessibilityLabel("Drag selected original files to CapCut")
            setAccessibilityRole(.button)
        }
        required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
        override func resetCursorRects() { addCursorRect(bounds, cursor: .openHand) }
        override func draw(_ dirtyRect: NSRect) {
            NSColor.controlAccentColor.withAlphaComponent(clips.isEmpty ? 0.2 : 0.85).setFill()
            NSBezierPath(roundedRect: bounds, xRadius: 8, yRadius: 8).fill()
            let text = "Drag \(clips.count) original\(clips.count == 1 ? "" : "s") to CapCut" as NSString
            let attributes: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: 12, weight: .semibold), .foregroundColor: NSColor.white]
            let size = text.size(withAttributes: attributes)
            text.draw(at: NSPoint(x: (bounds.width - size.width) / 2, y: (bounds.height - size.height) / 2), withAttributes: attributes)
        }
        override func mouseDown(with event: NSEvent) {}
        override func mouseDragged(with event: NSEvent) {
            guard !clips.isEmpty else { return }
            do {
                let urls = try ClipDragHandle.writers(clips)
                let items = urls.enumerated().map { index, url in
                    let item = NSDraggingItem(pasteboardWriter: url)
                    let icon = NSWorkspace.shared.icon(forFile: url.path ?? "")
                    item.setDraggingFrame(NSRect(x: index * 3, y: index * 3, width: 40, height: 40), contents: icon)
                    return item
                }
                let session = beginDraggingSession(with: items, event: event, source: self)
                session.draggingFormation = .stack
            } catch { failure(error.localizedDescription) }
        }
        func draggingSession(_ session: NSDraggingSession, sourceOperationMaskFor context: NSDraggingContext) -> NSDragOperation { .copy }
    }
}

struct SelectedClipRow: View {
    let clip: SelectedClip
    var projection: ProjectionReader? = nil
    let open: () -> Void
    @ViewState private var image: NSImage?
    var body: some View {
        Button(action: open) {
            HStack(spacing: 8) {
                Group {
                    if let image { Image(nsImage: image).resizable().scaledToFit() }
                    else { Image(systemName: "film") }
                }.frame(width: 58, height: 42).background(.black.opacity(0.5)).clipShape(RoundedRectangle(cornerRadius: 5))
                VStack(alignment: .leading) {
                    Text(clip.url.lastPathComponent).font(.caption).lineLimit(1).truncationMode(.middle)
                    Text("Full original").font(.caption2).foregroundStyle(.secondary)
                }
                Spacer(minLength: 0)
            }.contentShape(Rectangle())
        }.buttonStyle(.plain)
            .task(id: clip.media.match.cacheKey) { image = await FrameCache.shared.image(clip.media.match, assetID: clip.assetID, projection: projection, maxPixelSize: 120) }
    }
}

struct SelectionIsland: View {
    @ObservedObject var clips: ClipSelection
    var projection: ProjectionReader? = nil
    let open: (SelectedClip) -> Void
    let collapse: () -> Void
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("Selected clips").font(.headline)
                Text("\(clips.items.count)").monospacedDigit().foregroundStyle(.secondary)
                Spacer()
                Button(action: collapse) { Image(systemName: "sidebar.right") }.buttonStyle(.plain).help("Hide selected clips")
            }
            Text("Gather footage here, then drag it into your edit.").font(.caption).foregroundStyle(.secondary)
            if clips.items.isEmpty {
                Spacer()
                Label("Use + on a thumbnail", systemImage: "plus.circle").foregroundStyle(.secondary)
                Text("Your selection stays here as you search and filter.").font(.caption).foregroundStyle(.secondary)
                Spacer()
            } else {
                ScrollView {
                    LazyVStack(spacing: 12) {
                        ForEach(clips.items) { clip in
                            HStack(spacing: 6) {
                                SelectedClipRow(clip: clip, projection: projection) { open(clip) }
                                Button { clips.remove(clip.id) } label: { Image(systemName: "xmark.circle.fill") }
                                    .buttonStyle(.plain).foregroundStyle(.secondary).help("Remove from selection")
                            }.contextMenu {
                                Button("Move up") { clips.move(clip.id, by: -1) }
                                Button("Move down") { clips.move(clip.id, by: 1) }
                                Button("Reveal in Finder") { NSWorkspace.shared.activateFileViewerSelecting([clip.url]) }
                            }
                        }
                    }
                }
            }
            if let error = clips.error { Text(error).font(.caption).foregroundStyle(.orange) }
            ClipDragHandle(clips: clips.items) { clips.error = $0 }.frame(height: 38)
            HStack {
                Text("Saved until you clear it").font(.caption2).foregroundStyle(.secondary)
                Spacer()
                Button("Clear") { clips.clear() }.font(.caption).disabled(clips.items.isEmpty)
            }
        }.padding(14).frame(width: 260)
            .background(Color(red: 0.14, green: 0.15, blue: 0.17), in: RoundedRectangle(cornerRadius: 16))
            .overlay(RoundedRectangle(cornerRadius: 16).stroke(.white.opacity(0.08)))
            .padding(12)
    }
}
