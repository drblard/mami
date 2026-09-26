import SwiftUI
import AVKit

/// AVKit renders the picture; Mami owns the always-visible transport controls.
struct VideoSurface: NSViewRepresentable {
    let player: AVPlayer?
    func makeNSView(context: Context) -> AVPlayerView {
        let view = AVPlayerView()
        view.controlsStyle = .none
        view.videoGravity = .resizeAspect
        return view
    }
    func updateNSView(_ view: AVPlayerView, context: Context) { view.player = player }
}

@MainActor final class PlaybackTransport: ObservableObject {
    @Published private(set) var player: AVPlayer?
    @Published var position = 0.0
    @Published private(set) var duration = 0.0
    @Published private(set) var playing = false
    @Published var muted = false {
        didSet { player?.isMuted = muted }
    }
    private var observer: Any?
    private var editing = false
    private var seeking = false
    private var resumeAfterScrub = false
    private var seekGeneration = 0

    func attach(_ player: AVPlayer) {
        stop()
        self.player = player
        player.isMuted = muted
        let length = player.currentItem?.duration.seconds ?? 0
        duration = length.isFinite ? max(0, length) : 0
        position = player.currentTime().seconds
        observer = player.addPeriodicTimeObserver(forInterval: CMTime(seconds: 0.1, preferredTimescale: 600), queue: .main) { [weak self, weak player] time in
            Task { @MainActor [weak self, weak player] in
                guard let self, let player, self.player === player else { return }
                if !self.editing && !self.seeking && time.seconds.isFinite {
                    self.position = time.seconds
                }
                self.playing = player.rate != 0
            }
        }
    }

    func toggle() {
        guard let player else { return }
        if player.rate != 0 { player.pause(); playing = false }
        else if position >= duration - 0.1 { seek(to: 0, resume: true) }
        else { player.play(); playing = true }
    }

    func scrub(_ began: Bool) {
        if began {
            seekGeneration += 1
            seeking = false
            editing = true
            resumeAfterScrub = (player?.rate ?? 0) != 0
            player?.pause()
            playing = false
        } else {
            editing = false
            seek(to: position, resume: resumeAfterScrub)
        }
    }

    @discardableResult func seek(to value: Double, resume: Bool? = nil) -> Task<Void, Never>? {
        guard let player else { return nil }
        let shouldResume = resume ?? (player.rate != 0)
        position = min(max(0, value), duration)
        seeking = true
        seekGeneration += 1
        let generation = seekGeneration
        let target = position
        return Task {
            let ok = await player.seek(to: CMTime(seconds: target, preferredTimescale: 600), toleranceBefore: .zero, toleranceAfter: .zero)
            guard seekGeneration == generation else { return }
            seeking = false
            if ok {
                position = player.currentTime().seconds
                if shouldResume { player.play() } else { player.pause() }
                playing = player.rate != 0
            }
        }
    }

    func stop() {
        seekGeneration += 1
        if let observer { player?.removeTimeObserver(observer) }
        observer = nil
        player?.pause()
        player?.replaceCurrentItem(with: nil)
        player = nil
        playing = false
        editing = false
        seeking = false
    }
}

struct TransportBar: View {
    @ObservedObject var transport: PlaybackTransport
    var shortcutsEnabled = true
    var body: some View {
        HStack(spacing: 14) {
            Button { transport.seek(to: transport.position - 5) } label: { Image(systemName: "gobackward.5") }
                .keyboardShortcut(shortcutsEnabled ? KeyboardShortcut(.leftArrow, modifiers: []) : nil)
                .help("Back 5 seconds (←)")
            Button { transport.toggle() } label: {
                Image(systemName: transport.playing ? "pause.fill" : "play.fill").frame(width: 22)
            }.keyboardShortcut(shortcutsEnabled ? KeyboardShortcut(.space, modifiers: []) : nil).help("Play / pause (Space)")
            Button { transport.seek(to: transport.position + 5) } label: { Image(systemName: "goforward.5") }
                .keyboardShortcut(shortcutsEnabled ? KeyboardShortcut(.rightArrow, modifiers: []) : nil)
                .help("Forward 5 seconds (→)")
            Text(timeLabel(transport.position)).monospacedDigit().frame(width: 58)
            Slider(value: $transport.position, in: 0...max(0.001, transport.duration), onEditingChanged: transport.scrub)
                .accessibilityLabel("Video position")
            Text(timeLabel(transport.duration)).monospacedDigit().foregroundStyle(.secondary).frame(width: 58)
            Toggle(isOn: $transport.muted) { Image(systemName: transport.muted ? "speaker.slash.fill" : "speaker.wave.2.fill") }
                .toggleStyle(.button)
                .keyboardShortcut(shortcutsEnabled ? KeyboardShortcut("m", modifiers: []) : nil)
                .help("Mute audio (M)")
        }
        .buttonStyle(.plain)
        .padding(14)
        .background(Color(red: 0.14, green: 0.15, blue: 0.17), in: RoundedRectangle(cornerRadius: 12))
        .disabled(transport.player == nil)
    }
}
