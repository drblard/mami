import Foundation
import AVFoundation
import ImageIO
import UniformTypeIdentifiers

/// Bounded batches share one native decoder, preserving requested sample times.
enum VideoPreviews {
    static let maximumBatchSize = 16
    struct Request: Decodable {
        let timestamp: Double
        let target: String
    }

    static func generate(source: String, manifest: String) throws {
        let requests = try JSONDecoder().decode([Request].self, from: Data(contentsOf: URL(fileURLWithPath: manifest)))
        guard !requests.isEmpty, requests.count <= maximumBatchSize,
              requests.allSatisfy({ $0.timestamp.isFinite && $0.timestamp >= 0 }),
              Set(requests.map(\.target)).count == requests.count else {
            throw AppError.message("Invalid video preview batch")
        }
        let generator = AVAssetImageGenerator(asset: AVURLAsset(url: URL(fileURLWithPath: source)))
        generator.appliesPreferredTrackTransform = true
        generator.maximumSize = CGSize(width: 640, height: 640)
        generator.requestedTimeToleranceBefore = .zero
        generator.requestedTimeToleranceAfter = CMTime(seconds: 0.1, preferredTimescale: 600)
        var actualTimes: [Double] = []
        for request in requests {
            try autoreleasepool {
                var actual = CMTime.zero
                let image = try generator.copyCGImage(at: CMTime(seconds: request.timestamp, preferredTimescale: 60000), actualTime: &actual)
                guard actual.seconds.isFinite, abs(actual.seconds-request.timestamp) <= 0.11 else {
                    throw AppError.message("Decoder returned a frame outside the requested timestamp tolerance")
                }
                let encoded = NSMutableData()
                guard let destination = CGImageDestinationCreateWithData(encoded, UTType.jpeg.identifier as CFString, 1, nil) else {
                    throw AppError.message("Cannot create video preview encoder")
                }
                CGImageDestinationAddImage(destination, image, [kCGImageDestinationLossyCompressionQuality: 0.85] as CFDictionary)
                guard CGImageDestinationFinalize(destination) else { throw AppError.message("Cannot encode video preview") }
                try (encoded as Data).write(to: URL(fileURLWithPath: request.target), options: .withoutOverwriting)
                actualTimes.append(actual.seconds)
            }
        }
        let result = try JSONSerialization.data(withJSONObject: ["actual_times": actualTimes])
        print(String(decoding: result, as: UTF8.self))
    }
}
