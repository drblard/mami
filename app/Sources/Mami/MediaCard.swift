import SwiftUI
import AVKit
import ImageIO
import MamiCore

func timeLabel(_ seconds: Double?) -> String {
    guard let seconds else { return "Photo" }
    guard seconds.isFinite else { return "--:--" }
    let value = max(0, Int(seconds))
    if value >= 3600 { return String(format: "%d:%02d:%02d", value / 3600, value / 60 % 60, value % 60) }
    return String(format: "%02d:%02d", value / 60, value % 60)
}

/// Inputs are values so SwiftUI can skip unchanged cards (`.equatable()`); a card
/// observing the shared annotation/selection models redrew on every change anywhere.
struct MediaCard: View, @MainActor Equatable {
    let media: Media
    var projection: ProjectionReader? = nil
    let nearby: Bool
    var annotation = Annotation()
    var annotationsReady = true
    var inClips = false
    var clipsReady = true
    var focused = false
    var dragEnabled = true
    // Actions read current state when invoked, so they are not compared.
    var toggleFavorite: () -> Void = {}
    var toggleClip: (Sample) -> Void = { _ in }
    var select: () -> Void = {}
    var dragItems: () -> [Media] = { [] }
    let open: (Media, Double?) -> Void
    var trash: (() -> Void)? = nil

    static func == (a: MediaCard, b: MediaCard) -> Bool {
        a.media == b.media && a.projection == b.projection && a.nearby == b.nearby && a.annotation == b.annotation
            && a.annotationsReady == b.annotationsReady && a.inClips == b.inClips && a.clipsReady == b.clipsReady
            && a.focused == b.focused && a.dragEnabled == b.dragEnabled && (a.trash == nil) == (b.trash == nil)
    }
    @ViewState private var hovered: Int?
    @ViewState private var hoverFraction: CGFloat?
    @ViewState private var image: NSImage?
    @ViewState private var unavailable = false
    @ViewState private var pointerInside = false
    @ViewState private var loadedFrames: [Sample]?
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
                    Button(action: toggleFavorite) {
                        Image(systemName: annotation.favorite ? "heart.fill" : "heart")
                            .foregroundStyle(annotation.favorite ? Color.pink : Color.white)
                            .padding(7).background(.black.opacity(0.6), in: Circle())
                    }.buttonStyle(.plain).padding(7).disabled(!annotationsReady)
                        .help(annotation.favorite ? "Remove from favorites" : "Add to favorites")
                }
                .overlay(alignment: .topLeading) {
                    Button { toggleClip(sample) } label: {
                        Image(systemName: inClips ? "checkmark.circle.fill" : "plus.circle.fill")
                            .foregroundStyle(inClips ? Color.accentColor : Color.white)
                            .padding(7).background(.black.opacity(0.6), in: Circle())
                    }.buttonStyle(.plain).padding(7).disabled(!clipsReady)
                        .help(inClips ? "Remove from selected clips" : "Add to selected clips")
                        .accessibilityLabel(inClips ? "Remove from selected clips" : "Add to selected clips")
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
            if let trash {
                Divider()
                Button("Move to Trash", role: .destructive, action: trash)
            }
        }
        .help("Click to select · ⌘ click to add/remove · Drag originals · Space or double-click to preview")
    }
}

/// The displayed order for actions that run later (drags); not observed, so
/// updating it while rendering causes no further updates.
final class GridItems { var items: [Media] = [] }
