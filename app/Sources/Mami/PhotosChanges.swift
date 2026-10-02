import Foundation
import Photos

/// Which Photos assets an import pass must examine. A full pass looks up the
/// resources of every asset in the import range (~17k lookups, most of a pass's
/// CPU); later passes read the library's persistent change history and examine only
/// inserted or updated assets. A full pass still runs at launch, after any failed
/// pass, when the start date changes, and at least every `fullPassInterval`.
struct PhotosChangeTracker {
    static let fullPassInterval: TimeInterval = 6 * 60 * 60
    private let defaults: UserDefaults
    private static let tokenKey = "photos-import-change-token"
    private static let rangeKey = "photos-import-change-range-start"
    private static let fullPassKey = "photos-import-last-full-pass"

    init(defaults: UserDefaults = .standard) { self.defaults = defaults }

    /// Assets changed since the last recorded pass, or nil when a full pass is required.
    func changedAssets(in library: PHPhotoLibrary, rangeStart: Date, now: Date = Date()) -> Set<String>? {
        guard let data = defaults.data(forKey: Self.tokenKey),
              let token = try? NSKeyedUnarchiver.unarchivedObject(ofClass: PHPersistentChangeToken.self, from: data),
              defaults.object(forKey: Self.rangeKey) as? Date == rangeStart,
              let lastFull = defaults.object(forKey: Self.fullPassKey) as? Date,
              now.timeIntervalSince(lastFull) < Self.fullPassInterval else { return nil }
        do {
            var identifiers = Set<String>()
            for change in try library.fetchPersistentChanges(since: token) {
                let details = try change.changeDetails(for: .asset)
                identifiers.formUnion(details.insertedLocalIdentifiers)
                identifiers.formUnion(details.updatedLocalIdentifiers)
            }
            return identifiers
        } catch {
            return nil  // Expired or unavailable history: examine everything.
        }
    }

    /// Records a successful pass. `token` was read before the pass began, so
    /// changes made during it are examined again next time.
    func record(token: Data, rangeStart: Date, fullPass: Bool, now: Date = Date()) {
        defaults.set(token, forKey: Self.tokenKey)
        defaults.set(rangeStart, forKey: Self.rangeKey)
        if fullPass { defaults.set(now, forKey: Self.fullPassKey) }
    }

    /// Forces the next pass to examine every asset (launch, failures).
    func requireFullPass() { defaults.removeObject(forKey: Self.fullPassKey) }

    static func currentToken(of library: PHPhotoLibrary) -> Data? {
        try? NSKeyedArchiver.archivedData(withRootObject: library.currentChangeToken, requiringSecureCoding: true)
    }
}

/// Starts an import pass soon after the System Photo Library changes (e.g. an iCloud
/// sync delivered new items), instead of waiting for the next periodic pass.
final class PhotosLibraryObserver: NSObject, PHPhotoLibraryChangeObserver {
    private let changed: @Sendable () -> Void
    init(changed: @escaping @Sendable () -> Void) { self.changed = changed }
    func photoLibraryDidChange(_ changeInstance: PHChange) { changed() }
}
