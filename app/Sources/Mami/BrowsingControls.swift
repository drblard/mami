import SwiftUI
import AVFoundation
import ImageIO
import MamiCore

@MainActor @Observable final class BrowserSelection {
    var columns = 1
    var focused: Media?
    var preview: Selection?
    var selectedIDs = Set<String>()
    var previewItems: [Media]?
    func itemsForDrag(_ media: Media, in items: [Media]) -> [Media] {
        if !selectedIDs.contains(media.id) { select(media, extending: false) }
        return items.filter { selectedIDs.contains($0.id) }
    }
    func select(_ media: Media, extending: Bool) {
        if extending {
            if !selectedIDs.insert(media.id).inserted { selectedIDs.remove(media.id) }
        } else { selectedIDs = [media.id] }
        updateFocus(media)
    }
    /// Pointer clicks toggle a sole selection off; keyboard focus uses `select`.
    func click(_ media: Media, extending: Bool) {
        selectedIDs = GridClickSelection.click(media.id, selected: selectedIDs, extending: extending)
        updateFocus(media)
    }
    private func updateFocus(_ media: Media) {
        if selectedIDs.contains(media.id) { focused = media }
        else if focused?.id == media.id || selectedIDs.isEmpty { focused = nil }
    }
}

struct LibraryFooter: View {
    @Bindable var library: Library
    private var backups = CatalogBackups.shared
    private var searchMaintenance = SearchMaintenance.shared
    var body: some View {
        VStack(spacing: 0) {
            Divider().padding(.bottom, 4)
            HStack(spacing: 12) {
                Toggle("Lock grid", isOn: $library.gridLocked).toggleStyle(.checkbox).disabled(!library.ready)
                    .help("Hold this view while media is indexed. Refresh brings in new arrivals.")
                Text("\(library.pendingMediaCount) new media").monospacedDigit().opacity(library.gridLocked ? 1 : 0)
                Button { library.refreshGrid() } label: { Label("Refresh", systemImage: "arrow.clockwise") }
                Spacer()
                IndexQueueSummary(indexing: .previews)
                IndexQueueSummary()
                IndexQueueSummary(indexing: .faces)
                if let dates = library.queryDates { Text("Search dates: \(dates.label)").foregroundStyle(.secondary) }
                if let error = backups.error { Image(systemName: "exclamationmark.triangle").foregroundStyle(.orange).help(error) }
                if let error = searchMaintenance.error { Image(systemName: "exclamationmark.triangle").foregroundStyle(.orange).help(error) }
            }.font(.caption).padding(.horizontal, 14).frame(height: 24)
            IndexingBar(indexing: .previews)
            IndexingBar()
            IndexingBar(indexing: .faces)
        }.padding(.bottom, 4).background(Color(red: 0.11, green: 0.12, blue: 0.14))
    }
}

/// Observe drags without covering SwiftUI hover tracking and action buttons.
struct MediaDragSurface: NSViewRepresentable {
    var enabled: Bool
    var items: () -> [Media]
    func makeNSView(context: Context) -> DragView { DragView() }
    func updateNSView(_ view: DragView, context: Context) { view.items = items; view.enabled = enabled }
    final class DragView: NSView, NSDraggingSource {
        var items: () -> [Media] = { [] }
        var enabled = true
        fileprivate weak var router: MediaDragRouter?
        override func hitTest(_ point: NSPoint) -> NSView? { nil }
        override init(frame: NSRect) {
            super.init(frame: frame)
            MediaDragRouter.shared.register(self)
        }
        required init?(coder: NSCoder) { fatalError() }
        func draggingSession(_ session: NSDraggingSession, sourceOperationMaskFor context: NSDraggingContext) -> NSDragOperation { .copy }
        func draggingSession(_ session: NSDraggingSession, endedAt screenPoint: NSPoint, operation: NSDragOperation) {
            router?.draggingEnded()
        }
    }
}

/// One mouse-down owner for the whole grid. Per-card monitors can retain stale
/// gestures across native drag sessions, which do not deliver normal mouse-up.
@MainActor final class MediaDragRouter {
    static let shared = MediaDragRouter()
    private let views = NSHashTable<MediaDragSurface.DragView>.weakObjects()
    private weak var owner: MediaDragSurface.DragView?
    private var origin: CGPoint?
    private var provider: (() -> [Media])?
    private var monitor: Any?
    init() {
        monitor = NSEvent.addLocalMonitorForEvents(matching: [.leftMouseDown, .leftMouseDragged, .leftMouseUp]) { [weak self] event in
            guard let self else { return event }
            if event.type == .leftMouseDown { self.mouseDown(event); return event }
            if event.type == .leftMouseUp { self.reset(); return event }
            guard let (view, media) = self.dragItems(event) else { return event }
            guard !media.isEmpty, media.allSatisfy({ FileManager.default.isReadableFile(atPath: $0.url.path) }) else { NSSound.beep(); return event }
            let point = view.convert(event.locationInWindow, from: nil)
            let items = media.enumerated().map { index, media in
                let item = NSDraggingItem(pasteboardWriter: media.url as NSURL)
                item.setDraggingFrame(NSRect(x: point.x + CGFloat(index % 5) * 3, y: point.y, width: 40, height: 40), contents: NSWorkspace.shared.icon(forFile: media.url.path))
                return item
            }
            self.draggedMedia = media
            view.beginDraggingSession(with: items, event: event, source: view)
            return nil
        }
    }
    isolated deinit { if let monitor { NSEvent.removeMonitor(monitor) } }
    /// Media in the current in-app drag session. In-window drop targets use it
    /// instead of mapping file URLs back to catalog items.
    private(set) var draggedMedia: [Media] = []
    func draggingEnded() { draggedMedia = [] }
    func register(_ view: MediaDragSurface.DragView) { views.add(view); view.router = self }
    private func reset() { owner = nil; origin = nil; provider = nil }
    func mouseDown(_ event: NSEvent) {
        reset()
        guard let window = event.window, window.attachedSheet == nil else { return }
        let candidates = views.allObjects.filter { view in
            guard view.enabled, view.window == window, !view.isHiddenOrHasHiddenAncestor else { return false }
            let point = view.convert(event.locationInWindow, from: nil)
            let top = view.isFlipped ? point.y < 52 : point.y > view.bounds.height - 52
            let actionButton = top && (point.x < 52 || point.x > view.bounds.width - 52)
            // AppKit views need not clip to bounds: visibleRect may extend over
            // neighboring cards. Both rectangles must contain the mouse-down.
            return view.bounds.contains(point) && view.visibleRect.contains(point) && !actionButton
        }
        if CommandLine.arguments.contains("--ui-test") {
            print("DRAG ROUTE point=\(event.locationInWindow) candidates=\(candidates.count) views=\(views.allObjects.map { "\($0.frame)/\($0.visibleRect)/\($0.window === window)" })")
        }
        // Never export another card if layout happens to supply overlapping regions.
        guard candidates.count == 1, let view = candidates.first else { return }
        owner = view; origin = event.locationInWindow; provider = view.items
    }
    func dragItems(_ event: NSEvent) -> (MediaDragSurface.DragView, [Media])? {
        guard let view = owner, view.enabled, view.window == event.window,
              let origin, let provider,
              hypot(event.locationInWindow.x - origin.x, event.locationInWindow.y - origin.y) >= 5 else { return nil }
        // Clear before selection publishes a SwiftUI update or native dragging starts.
        reset()
        return (view, provider())
    }
}

enum GridNavigation {
    static func target(index: Int, key: UInt16, count: Int, columns: Int) -> Int {
        let width = max(1, columns)
        switch key {
        case 126: return index >= width ? index - width : index
        case 125:
            guard (index / width + 1) * width < count else { return index }
            return min(count - 1, index + width)
        case 123: return max(0, index - 1)
        default: return min(count - 1, index + 1)
        }
    }
}

struct BrowserKeys: NSViewRepresentable {
    var command: (Bool) -> Void
    var key: (UInt16) -> Bool
    var mouse: (CGPoint, CGSize) -> Bool = { _, _ in false }
    func makeNSView(context: Context) -> KeyView { KeyView() }
    func updateNSView(_ view: KeyView, context: Context) { view.command = command; view.key = key; view.mouse = mouse }
    final class KeyView: NSView {
        var command: (Bool) -> Void = { _ in }
        var key: (UInt16) -> Bool = { _ in false }
        var mouse: (CGPoint, CGSize) -> Bool = { _, _ in false }
        private var monitor: Any?
        override init(frame: NSRect) {
            super.init(frame: frame)
            monitor = NSEvent.addLocalMonitorForEvents(matching: [.keyDown, .flagsChanged, .leftMouseDown]) { [weak self] event in
                guard let self, let window = self.window else { return event }
                if event.type == .flagsChanged {
                    if window.isKeyWindow { self.command(event.modifierFlags.contains(.command)) }
                    return event
                }
                guard event.window == window else { return event }
                if event.type == .leftMouseDown {
                    guard window.attachedSheet == nil, let content = window.contentView else { return event }
                    let point = content.convert(event.locationInWindow, from: nil)
                    let handled = content.bounds.contains(point) && self.mouse(point, content.bounds.size)
                    if CommandLine.arguments.contains("--ui-test") { print("BROWSER MOUSE \(point), window=\(event.locationInWindow), frame=\(content.frame), outside-dismiss=\(handled)") }
                    return handled ? nil : event
                }
                guard !(window.firstResponder is NSTextView), window.attachedSheet == nil,
                      event.modifierFlags.intersection([.command, .control, .option]).isEmpty else { return event }
                // A layout-independent '?' command, outside text entry only.
                return self.key(event.characters == "?" ? 191 : event.keyCode) ? nil : event
            }
        }
        required init?(coder: NSCoder) { fatalError() }
        isolated deinit { if let monitor { NSEvent.removeMonitor(monitor) } }
    }
}

struct ShortcutHelp: View {
    let close: () -> Void
    private let shortcuts = [
        ("?", "Show keyboard shortcuts"),
        ("⌘ F", "Focus search"),
        ("Return", "Search immediately"),
        ("⌘ click", "Add or remove a media selection"),
        ("Space", "Open or close preview"),
        ("←  →", "Previous or next media"),
        ("↑  ↓", "Move by a row in the grid"),
        ("B", "Add/remove highlighted media in Selected clips"),
        ("⌘ ⌫", "Move highlighted or previewed media to the Trash"),
        ("M", "Mute/unmute video in preview"),
        ("Esc", "Close preview or popup"),
        ("⌘ hover", "Invert thumbnail scrubbing scope"),
        ("⌘ ,", "Open Settings")
    ]
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Keyboard shortcuts").font(.headline)
            ForEach(shortcuts, id: \.0) { key, description in
                HStack(spacing: 18) {
                    Text(key).font(.system(.body, design: .monospaced)).frame(width: 85, alignment: .trailing)
                    Text(description).frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            Divider()
            Text("Click to select · Double-click to preview · Drag selected originals into your editor.")
                .font(.caption).foregroundStyle(.secondary)
            HStack { Spacer(); Button("Done", action: close).keyboardShortcut(.cancelAction) }
        }.padding(20).frame(width: 440)
    }
}

enum PreviewSizing {
    static func dimensions(_ media: Media) async -> CGSize {
        if media.kind == "video" {
            do {
                let asset = AVURLAsset(url: media.url)
                guard let track = try await asset.loadTracks(withMediaType: .video).first else { return .zero }
                let size = try await track.load(.naturalSize)
                let transform = try await track.load(.preferredTransform)
                let rect = CGRect(origin: .zero, size: size).applying(transform)
                return CGSize(width: abs(rect.width), height: abs(rect.height))
            } catch { return .zero }
        }
        return await Task.detached(priority: .userInitiated) {
            guard let source = CGImageSourceCreateWithURL(media.url as CFURL, nil),
                  let values = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
                  let width = values[kCGImagePropertyPixelWidth] as? NSNumber,
                  let height = values[kCGImagePropertyPixelHeight] as? NSNumber else { return CGSize.zero }
            let rotated = (5...8).contains((values[kCGImagePropertyOrientation] as? NSNumber)?.intValue ?? 1)
            return CGSize(width: rotated ? height.doubleValue : width.doubleValue, height: rotated ? width.doubleValue : height.doubleValue)
        }.value
    }
    static func fit(_ pixels: CGSize, into area: CGSize, scale: CGFloat) -> CGFloat {
        guard pixels.width > 0, pixels.height > 0 else { return 1 }
        return min(1, max(0.001, min(area.width * scale / pixels.width, area.height * scale / pixels.height)))
    }
}

struct TagFilterPicker: View {
    let available: [String]
    @Binding var selected: Set<String>
    @ViewState private var open = false
    @ViewState private var query = ""
    private var suggestions: [String] { available.filter { query.isEmpty || $0.localizedStandardContains(query) } }
    var body: some View {
        Button { open.toggle() } label: {
            Label(selected.isEmpty ? "Tags & places" : "Tags & places (\(selected.count))", systemImage: "tag")
        }.popover(isPresented: $open) {
            VStack(alignment: .leading, spacing: 12) {
                Text("Filter tags & places").font(.headline)
                TextField("Type a tag or place…", text: $query).textFieldStyle(.roundedBorder)
                    .onSubmit { if let first = suggestions.first { selected.insert(first); query = "" } }
                if available.isEmpty { Text("Add tags or a place from a media preview to organize your library.").foregroundStyle(.secondary) }
                else if suggestions.isEmpty { Text("No matching tags or places").foregroundStyle(.secondary) }
                ScrollView {
                    LazyVStack(alignment: .leading) {
                        ForEach(suggestions, id: \.self) { label in
                            Toggle(label, isOn: Binding(get: { selected.contains(label) }, set: { enabled in
                                if enabled { selected.insert(label) } else { selected.remove(label) }
                            }))
                        }
                    }.frame(maxWidth: .infinity, alignment: .leading)
                }.frame(maxHeight: 220)
                HStack { Button("Clear") { selected = []; query = "" }; Spacer(); Button("Done") { open = false } }
            }.padding(16).frame(width: 300)
        }
    }
}
