import Foundation

/// An append-only diagnostic log in the cache directory, rotated to `<name>.1` at
/// `limit` bytes. Writes run on a utility queue, never on the caller's thread.
struct BoundedLog: Sendable {
    static let limit = 1024 * 1024
    let name: String
    private let writer: DispatchQueue

    init(_ name: String) {
        self.name = name
        writer = DispatchQueue(label: "local.mami.log.\(name)", qos: .utility)
    }

    var file: URL { AppPaths().caches.appendingPathComponent(name) }

    func write(_ message: String) {
        let line = "\(Date().formatted(.iso8601)) \(message)\n", url = file
        writer.async { Self.append(line, to: url) }
    }

    private static func append(_ line: String, to url: URL) {
        try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        if let size = (try? FileManager.default.attributesOfItem(atPath: url.path)[.size]) as? Int, size >= limit {
            try? FileManager.default.removeItem(at: url.appendingPathExtension("1"))
            try? FileManager.default.moveItem(at: url, to: url.appendingPathExtension("1"))
        }
        if let handle = try? FileHandle(forWritingTo: url) {
            defer { try? handle.close() }
            _ = try? handle.seekToEnd(); try? handle.write(contentsOf: Data(line.utf8))
        } else { try? Data(line.utf8).write(to: url) }
    }
}
