import AppKit
import OSLog

/// macOS fetches new iCloud Photos promptly only while the Photos app runs; otherwise
/// its background sync can lag by hours (observed 2026-10-01: about two hours). PhotoKit
/// offers no way to request a sync, so automatic import keeps Photos open, hidden.
/// Checked before every automatic import pass, which also relaunches it after a quit.
@MainActor enum PhotosSync {
    static let bundleIdentifier = "com.apple.Photos"
    nonisolated private static let log = Logger(subsystem: "local.mami.prototype", category: "PhotosImport")

    static func keepRunning(workspace: NSWorkspace = .shared) {
        guard !workspace.runningApplications.contains(where: { $0.bundleIdentifier == bundleIdentifier }),
              let application = workspace.urlForApplication(withBundleIdentifier: bundleIdentifier) else { return }
        let configuration = NSWorkspace.OpenConfiguration()
        configuration.activates = false
        configuration.hides = true
        configuration.addsToRecentItems = false
        workspace.openApplication(at: application, configuration: configuration) { _, error in
            if let error { log.error("Could not start Photos for iCloud sync: \(error.localizedDescription, privacy: .public)") }
            else { log.notice("Started Photos hidden so iCloud Photos syncs promptly") }
        }
    }
}
