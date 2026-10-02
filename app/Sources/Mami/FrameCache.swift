import SwiftUI
import AVKit
import ImageIO
import MamiCore

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
