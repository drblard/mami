import Foundation

/// Captures the app's own fatal-error text (Swift writes it to stderr before
/// trapping), which crash reports omit. Workers keep the original stderr so their
/// diagnostics do not fill the file.
enum AppDiagnostics {
    static let errorLogLimit = 1024 * 1024
    /// Set once by `captureAppErrors()` at launch, before any worker starts; read-only afterwards.
    nonisolated(unsafe) private(set) static var workerErrorOutput = FileHandle.standardError

    static func captureAppErrors() {
        let url = AppPaths().caches.appendingPathComponent("app-errors.log")
        try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        if let size = (try? FileManager.default.attributesOfItem(atPath: url.path)[.size]) as? Int, size >= errorLogLimit {
            try? FileManager.default.removeItem(at: url.appendingPathExtension("1"))
            try? FileManager.default.moveItem(at: url, to: url.appendingPathExtension("1"))
        }
        let original = dup(STDERR_FILENO)
        guard original >= 0, freopen(url.path, "a", stderr) != nil else { return }
        setvbuf(stderr, nil, _IONBF, 0)
        workerErrorOutput = FileHandle(fileDescriptor: original, closeOnDealloc: true)
        fputs("\(ISO8601DateFormatter().string(from: Date())) Mami started (pid \(getpid()))\n", stderr)
    }
}
