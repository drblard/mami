import AppKit
import Foundation

@MainActor func checkPreviewCache(at root: URL) async throws {
    let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 512, pixelsHigh: 512,
                                  bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
                                  isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    let redColor = NSColor(deviceRed: 1, green: 0, blue: 0, alpha: 1)
    let blueColor = NSColor(deviceRed: 0, green: 0, blue: 1, alpha: 1)
    let greenColor = NSColor(deviceRed: 0, green: 1, blue: 0, alpha: 1)
    let yellowColor = NSColor(deviceRed: 1, green: 1, blue: 0, alpha: 1)
    for y in 0..<512 {
        for x in 0..<512 {
            bitmap.setColor(x < 256 ? (y < 256 ? redColor : blueColor) : (y < 256 ? greenColor : yellowColor), atX: x, y: y)
        }
    }
    let atlas = root.appendingPathComponent("atlas-test.jpg")
    try bitmap.representation(using: .jpeg, properties: [.compressionFactor: 0.9])!.write(to: atlas, options: .withoutOverwriting)
    let first = Sample(path: "asset-001", kind: "video", timestamp: 0, frame: atlas.path, score: nil, evidence: nil, crop: [0,0,256,256])
    let second = Sample(path: "asset-001", kind: "video", timestamp: 1, frame: atlas.path, score: nil, evidence: nil, crop: [0,256,256,256])
    guard first.cacheKey != second.cacheKey,
          let red = await FrameCache.shared.image(first.frame, crop: first.crop),
          let blue = await FrameCache.shared.image(second.frame, crop: second.crop) else {
        throw AppError.message("Packed crops were not decoded independently")
    }
    func color(_ image: NSImage) throws -> NSColor {
        guard let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil), cg.width == 256, cg.height == 256,
              let value = NSBitmapImageRep(cgImage: cg).colorAt(x: 128, y: 128)?.usingColorSpace(.deviceRGB) else {
            throw AppError.message("Packed preview crop has incorrect dimensions")
        }
        return value
    }
    let r = try color(red), b = try color(blue)
    guard r.redComponent > 0.9, r.blueComponent < 0.1, b.blueComponent > 0.9, b.redComponent < 0.1 else {
        throw AppError.message("Packed preview crops show the wrong cells: first=\(r), second=\(b)")
    }
    let db = try SQLDatabase(root.appendingPathComponent("projection-reader.sqlite"))
    try db.execute("UPDATE frames SET frame=?,crop=? WHERE asset='asset-001'", [atlas.path,"[0,256,256,256]"])
    let stale = Sample(path: "asset-001", kind: "video", timestamp: 0.5, frame: root.appendingPathComponent("retired.jpg").path, score: nil, evidence: nil)
    let projection = ProjectionReader(database: root.appendingPathComponent("projection-reader.sqlite"))
    guard let recovered = await FrameCache.shared.image(stale, assetID: "asset-001", projection: projection),
          try color(recovered).blueComponent > 0.9 else { throw AppError.message("Stale cache reference did not reconnect to its packed frame") }
    guard await FrameCache.shared.image(atlas.path, crop: [500,0,256,256]) == nil else {
        throw AppError.message("Invalid crop bounds were accepted")
    }
    print("PREVIEW CACHE TEST PASSED: distinct crops, pixel colors, dimensions, stale-reference recovery and invalid bounds")
}
