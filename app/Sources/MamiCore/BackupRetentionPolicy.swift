import Foundation

/// The on-disk receipt format is shared by full exports and personal backups.
public struct CatalogSnapshot: Codable, Sendable {
    public let identity: String
    public let revision: String
    public let changeToken: String
    public let file: String
    public let created: Date

    public init(identity: String, revision: String, changeToken: String, file: String, created: Date) {
        self.identity = identity
        self.revision = revision
        self.changeToken = changeToken
        self.file = file
        self.created = created
    }
}

/// Pure retention rules. File validation and deletion belong to the storage layer.
public struct BackupRetentionPolicy: Sendable {
    public static let standard = Self(
        recentSnapshotCount: 64,
        hourlyWindowHours: 24,
        dailyWindowDays: 30,
        monthlyWindowMonths: 12
    )

    public let recentSnapshotCount: Int
    public let hourlyWindowHours: Int
    public let dailyWindowDays: Int
    public let monthlyWindowMonths: Int

    public init(recentSnapshotCount: Int, hourlyWindowHours: Int, dailyWindowDays: Int, monthlyWindowMonths: Int) {
        precondition([recentSnapshotCount, hourlyWindowHours, dailyWindowDays, monthlyWindowMonths].allSatisfy { $0 >= 0 })
        self.recentSnapshotCount = recentSnapshotCount
        self.hourlyWindowHours = hourlyWindowHours
        self.dailyWindowDays = dailyWindowDays
        self.monthlyWindowMonths = monthlyWindowMonths
    }

    /// Windows include their cutoff instant. Each UTC calendar bucket retains
    /// its newest snapshot. The current receipt and other catalogs are protected.
    /// Future timestamps are preserved separately in case the clock moved back.
    public func retainedFiles(in snapshots: [CatalogSnapshot], preserving current: CatalogSnapshot, now: Date) -> Set<String> {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(secondsFromGMT: 0)!

        let eligible = snapshots.filter { $0.identity == current.identity && $0.created <= now }.sorted {
            $0.created == $1.created ? $0.file < $1.file : $0.created > $1.created
        }
        var retained = Set(snapshots.filter { $0.identity != current.identity || $0.created > now }.map(\.file))
        retained.formUnion(eligible.prefix(recentSnapshotCount).map(\.file))
        retained.insert(current.file)

        let tiers: [(component: Calendar.Component, count: Int)] = [
            (.hour, hourlyWindowHours),
            (.day, dailyWindowDays),
            (.month, monthlyWindowMonths)
        ]
        for tier in tiers where tier.count > 0 {
            guard let cutoff = calendar.date(byAdding: tier.component, value: -tier.count, to: now) else {
                // A calendar/clock value that cannot be represented must not
                // turn into permission to discard recovery points.
                return Set(snapshots.map(\.file)).union([current.file])
            }
            var buckets = Set<Date>()
            for snapshot in eligible where snapshot.created >= cutoff {
                guard let bucket = calendar.dateInterval(of: tier.component, for: snapshot.created)?.start else {
                    retained.insert(snapshot.file)
                    continue
                }
                if buckets.insert(bucket).inserted { retained.insert(snapshot.file) }
            }
        }
        return retained
    }
}
