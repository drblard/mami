import Foundation
import ImageIO
import UniformTypeIdentifiers

/// Use macOS's HEIC/JPEG/PNG decoder, including display orientation and primary
/// image selection, instead of treating HEIF auxiliary images as video streams.
enum ImageDecoding {
    static func source(_ path: String) throws -> CGImageSource {
        guard let source = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil) else {
            throw AppError.message("Cannot open image: \(URL(fileURLWithPath: path).lastPathComponent)")
        }
        return source
    }
    static func metadata(_ path: String) throws -> [String: Any] {
        let source = try source(path)
        let index = CGImageSourceGetPrimaryImageIndex(source)
        guard let properties = CGImageSourceCopyPropertiesAtIndex(source, index, nil) as? [String: Any] else {
            throw AppError.message("Cannot read image metadata")
        }
        let tiff = properties[kCGImagePropertyTIFFDictionary as String] as? [String: Any] ?? [:]
        let exif = properties[kCGImagePropertyExifDictionary as String] as? [String: Any] ?? [:]
        let gps = properties[kCGImagePropertyGPSDictionary as String] as? [String: Any] ?? [:]
        let width = (properties[kCGImagePropertyPixelWidth as String] as? NSNumber)?.doubleValue ?? 0
        let height = (properties[kCGImagePropertyPixelHeight as String] as? NSNumber)?.doubleValue ?? 0
        let camera = [tiff["Make"], tiff["Model"]].compactMap { $0 as? String }.filter { !$0.isEmpty }.joined(separator: " ")
        var details = [String(format: "%.1f MP", width * height / 1_000_000)]
        if !camera.isEmpty { details.append(camera) }
        var result: [String: Any] = ["date": "Capture date unavailable", "sortDate": "", "details": details,
                                    "tags": [String](), "technical": [String](), "camera": camera]
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyy:MM:dd HH:mm:ss"
        if let raw = (exif["DateTimeOriginal"] ?? tiff["DateTime"]) as? String, let date = formatter.date(from: raw) {
            formatter.dateFormat = "dd MMM yyyy · HH:mm"
            result["date"] = formatter.string(from: date)
            formatter.dateFormat = "yyyyMMddHHmmss"
            result["sortDate"] = formatter.string(from: date)
        }
        if let lat = gps["Latitude"] as? NSNumber, let lon = gps["Longitude"] as? NSNumber,
           let latRef = gps["LatitudeRef"] as? String, let lonRef = gps["LongitudeRef"] as? String,
           lat.doubleValue != 0 || lon.doubleValue != 0 {
            result["location"] = String(format: "%.3f°%@ %.3f°%@", lat.doubleValue, latRef, lon.doubleValue, lonRef)
        }
        var technical: [String] = []
        if let iso = (exif["ISOSpeedRatings"] as? [NSNumber])?.first { technical.append("ISO \(iso)") }
        if let aperture = exif["FNumber"] as? NSNumber { technical.append("ƒ/ \(aperture)") }
        if let focal = exif["FocalLenIn35mmFilm"] as? NSNumber { technical.append("mm equivalent \(focal)") }
        result["technical"] = technical
        return result
    }
    static func preview(_ path: String, to target: String) throws {
        guard !FileManager.default.fileExists(atPath: target) else { throw AppError.message("Preview target already exists") }
        let source = try source(path)
        guard let image = CGImageSourceCreateThumbnailAtIndex(source, CGImageSourceGetPrimaryImageIndex(source), [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceThumbnailMaxPixelSize: 640
        ] as CFDictionary),
              let output = CGImageDestinationCreateWithURL(URL(fileURLWithPath: target) as CFURL, UTType.jpeg.identifier as CFString, 1, nil) else {
            throw AppError.message("macOS could not decode image preview")
        }
        CGImageDestinationAddImage(output, image, [kCGImageDestinationLossyCompressionQuality: 0.85] as CFDictionary)
        guard CGImageDestinationFinalize(output) else { throw AppError.message("Cannot save image preview") }
    }
}
