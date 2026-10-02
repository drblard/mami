import SwiftUI
import AVKit
import ImageIO
import MamiCore

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
    var annotations: Annotations
    var clips: ClipSelection
    var close: () -> Void = {}
    var position: String = ""
    var trash: (() -> Void)? = nil
    @ViewState private var transport = PlaybackTransport()
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
                if let trash {
                    Button(role: .destructive, action: trash) { Image(systemName: "trash") }
                        .help("Move to Trash (⌘⌫)").accessibilityLabel("Move to Trash")
                }
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
