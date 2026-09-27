import SwiftUI
import AVFoundation
import ImageIO

@MainActor final class BrowserSelection: ObservableObject {
    @Published var columns = 1
    @Published var focused: Media?
    @Published var preview: Selection?
    @Published var selectedIDs = Set<String>()
    var previewItems: [Media]?
    func itemsForDrag(_ media: Media, in items: [Media]) -> [Media] {
        if !selectedIDs.contains(media.id) { select(media, extending: false) }
        return items.filter { selectedIDs.contains($0.id) }
    }
    func select(_ media: Media, extending: Bool) {
        if extending {
            if !selectedIDs.insert(media.id).inserted { selectedIDs.remove(media.id) }
        } else { selectedIDs = [media.id] }
        if selectedIDs.contains(media.id) { focused = media }
        else if focused?.id == media.id || selectedIDs.isEmpty { focused = nil }
    }
}

struct LibraryFooter: View {
    @ObservedObject var library: Library
    @ObservedObject private var backups = CatalogBackups.shared
    var body: some View {
        VStack(spacing: 8) {
            Divider()
            HStack(spacing: 12) {
                Toggle("Lock grid", isOn: $library.gridLocked).toggleStyle(.checkbox).disabled(!library.ready)
                    .help("Hold this view while media is indexed. Refresh brings in new arrivals.")
                Text("\(library.pendingMediaCount) new media").monospacedDigit().opacity(library.gridLocked ? 1 : 0)
                Button { library.refreshGrid() } label: { Label("Refresh", systemImage: "arrow.clockwise") }
                Spacer()
                if let dates = library.queryDates { Text("Search dates: \(dates.label)").foregroundStyle(.secondary) }
                if let error = backups.error { Image(systemName: "exclamationmark.triangle").foregroundStyle(.orange).help(error) }
            }.font(.caption).padding(.horizontal, 14).frame(height: 24)
            IndexingBar()
        }.background(Color(red: 0.11, green: 0.12, blue: 0.14))
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
        private var monitor: Any?
        private var origin: CGPoint?
        override func hitTest(_ point: NSPoint) -> NSView? { nil }
        override init(frame: NSRect) {
            super.init(frame: frame)
            monitor = NSEvent.addLocalMonitorForEvents(matching: [.leftMouseDown, .leftMouseDragged, .leftMouseUp]) { [weak self] event in
                guard let self, self.enabled, let window = self.window, event.window == window else { return event }
                if event.type == .leftMouseUp { self.origin = nil; return event }
                if event.type == .leftMouseDown {
                    let point = self.convert(event.locationInWindow, from: nil)
                    let corner = point.y > self.bounds.height - 52 && (point.x < 52 || point.x > self.bounds.width - 52)
                    self.origin = self.visibleRect.contains(point) && !corner && window.attachedSheet == nil ? event.locationInWindow : nil
                    return event
                }
                guard let origin = self.origin, hypot(event.locationInWindow.x - origin.x, event.locationInWindow.y - origin.y) >= 5 else { return event }
                self.origin = nil
                let media = self.items()
                guard !media.isEmpty, media.allSatisfy({ FileManager.default.isReadableFile(atPath: $0.url.path) }) else { NSSound.beep(); return event }
                let point = self.convert(event.locationInWindow, from: nil)
                let dragging = media.enumerated().map { index, media in
                    let item = NSDraggingItem(pasteboardWriter: media.url as NSURL)
                    item.setDraggingFrame(NSRect(x: point.x + CGFloat(index % 5) * 3, y: point.y, width: 40, height: 40), contents: NSWorkspace.shared.icon(forFile: media.url.path))
                    return item
                }
                self.beginDraggingSession(with: dragging, event: event, source: self)
                return nil
            }
        }
        required init?(coder: NSCoder) { fatalError() }
        deinit { if let monitor { NSEvent.removeMonitor(monitor) } }
        func draggingSession(_ session: NSDraggingSession, sourceOperationMaskFor context: NSDraggingContext) -> NSDragOperation { .copy }
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
                return self.key(event.keyCode) ? nil : event
            }
        }
        required init?(coder: NSCoder) { fatalError() }
        deinit { if let monitor { NSEvent.removeMonitor(monitor) } }
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
