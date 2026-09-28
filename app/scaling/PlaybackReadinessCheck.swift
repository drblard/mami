import Foundation
import AVFoundation

/// Read-only decode/seek check. This does not measure SwiftUI rendering or
/// continuous playback through a visible AVPlayerView on an unlocked desktop.
@main struct PlaybackReadinessCheck {
    static func main() async throws {
        let input = URL(fileURLWithPath: CommandLine.arguments[1])
        let output = URL(fileURLWithPath: CommandLine.arguments[2])
        let files = try FileManager.default.contentsOfDirectory(at: input, includingPropertiesForKeys: nil)
            .filter { $0.pathExtension.lowercased() == "mp4" }.sorted { $0.path < $1.path }
        var results: [[String: Any]] = []
        for file in files {
            let started = ContinuousClock.now
            let asset = AVURLAsset(url: file)
            let playable = try await asset.load(.isPlayable)
            let duration = try await asset.load(.duration).seconds
            guard playable, duration.isFinite, duration > 0 else {
                throw NSError(domain: "PlaybackCheck", code: 1, userInfo: [NSLocalizedDescriptionKey: "Unplayable clip: \(file.lastPathComponent)"])
            }
            let generator = AVAssetImageGenerator(asset: asset)
            generator.appliesPreferredTrackTransform = true
            generator.maximumSize = CGSize(width: 960, height: 540)
            var seeks: [[String: Double]] = []
            for fraction in [0.0, 0.5, 0.95] {
                let before = ContinuousClock.now
                let requested = duration * fraction
                _ = try await generator.image(at: CMTime(seconds: requested, preferredTimescale: 600))
                let elapsed = before.duration(to: .now).components
                seeks.append(["timestamp": requested, "seconds": Double(elapsed.seconds) + Double(elapsed.attoseconds)/1e18])
            }
            let elapsed = started.duration(to: .now).components
            results.append(["file": file.lastPathComponent, "playable": playable, "duration": duration,
                            "decode_seeks": seeks, "total_seconds": Double(elapsed.seconds) + Double(elapsed.attoseconds)/1e18])
        }
        let data = try JSONSerialization.data(withJSONObject: ["files": results, "scope": "AVFoundation playability and original-frame decoding at start/middle/end; not UI playback timing"], options: [.prettyPrinted, .sortedKeys])
        try data.write(to: output, options: .withoutOverwriting)
        print(String(decoding: data, as: UTF8.self))
    }
}
