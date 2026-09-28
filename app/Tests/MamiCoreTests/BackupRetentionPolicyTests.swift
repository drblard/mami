import Foundation
import Testing
import MamiCore

struct BackupRetentionPolicyTests {
    private func date(_ text: String) -> Date {
        ISO8601DateFormatter().date(from: text)!
    }

    private func snapshot(_ name: String, _ timestamp: String, identity: String = "library") -> CatalogSnapshot {
        CatalogSnapshot(identity: identity, revision: name, changeToken: name, file: name, created: date(timestamp))
    }

    @Test func recentLimitProtectsCurrentSnapshotEvenWhenItIsOld() {
        let policy = BackupRetentionPolicy(recentSnapshotCount: 2, hourlyWindowHours: 0, dailyWindowDays: 0, monthlyWindowMonths: 0)
        let oldest = snapshot("current", "2026-09-28T09:00:00Z")
        let snapshots = [oldest, snapshot("discard", "2026-09-28T10:00:00Z"),
                         snapshot("recent", "2026-09-28T11:00:00Z"), snapshot("newest", "2026-09-28T12:00:00Z")]
        #expect(policy.retainedFiles(in: snapshots, preserving: oldest, now: date("2026-09-28T12:30:00Z")) ==
                Set(["current", "recent", "newest"]))
    }

    @Test func hourlyBucketsKeepNewestAndIncludeWindowCutoff() {
        let policy = BackupRetentionPolicy(recentSnapshotCount: 0, hourlyWindowHours: 24, dailyWindowDays: 0, monthlyWindowMonths: 0)
        let current = snapshot("current", "2026-09-28T12:20:00Z")
        let snapshots = [current, snapshot("same-hour", "2026-09-28T12:05:00Z"),
                         snapshot("previous-hour", "2026-09-28T11:55:00Z"), snapshot("older-in-hour", "2026-09-28T11:05:00Z"),
                         snapshot("cutoff", "2026-09-27T12:30:00Z"), snapshot("expired", "2026-09-27T12:29:59Z")]
        #expect(policy.retainedFiles(in: snapshots, preserving: current, now: date("2026-09-28T12:30:00Z")) ==
                Set(["current", "previous-hour", "cutoff"]))
    }

    @Test func dailyBucketsUseUTCDatesAndIncludeWindowCutoff() {
        let policy = BackupRetentionPolicy(recentSnapshotCount: 0, hourlyWindowHours: 0, dailyWindowDays: 30, monthlyWindowMonths: 0)
        let current = snapshot("current", "2026-09-28T12:20:00Z")
        let snapshots = [current, snapshot("earlier-today", "2026-09-28T00:05:00Z"),
                         snapshot("yesterday", "2026-09-27T23:55:00Z"), snapshot("earlier-yesterday", "2026-09-27T00:05:00Z"),
                         snapshot("cutoff", "2026-08-29T12:30:00Z"), snapshot("expired", "2026-08-29T12:29:59Z")]
        #expect(policy.retainedFiles(in: snapshots, preserving: current, now: date("2026-09-28T12:30:00Z")) ==
                Set(["current", "yesterday", "cutoff"]))
    }

    @Test func monthlyWindowUsesCalendarMonthsAcrossLeapYear() {
        let policy = BackupRetentionPolicy(recentSnapshotCount: 0, hourlyWindowHours: 0, dailyWindowDays: 0, monthlyWindowMonths: 12)
        let current = snapshot("current", "2025-03-01T12:00:00Z")
        let snapshots = [current, snapshot("february", "2025-02-28T20:00:00Z"),
                         snapshot("earlier-february", "2025-02-01T12:00:00Z"),
                         snapshot("cutoff", "2024-03-01T12:00:00Z"), snapshot("leap-day-expired", "2024-02-29T23:59:59Z")]
        #expect(policy.retainedFiles(in: snapshots, preserving: current, now: date("2025-03-01T12:00:00Z")) ==
                Set(["current", "february", "cutoff"]))
    }

    @Test func clockRollbackAndOtherLibrariesAreProtected() {
        let policy = BackupRetentionPolicy(recentSnapshotCount: 0, hourlyWindowHours: 0, dailyWindowDays: 0, monthlyWindowMonths: 0)
        let current = snapshot("current", "2026-09-28T10:00:00Z")
        let snapshots = [current, snapshot("future", "2026-09-29T10:00:00Z"),
                         snapshot("other-library", "2020-01-01T00:00:00Z", identity: "other"),
                         snapshot("expired", "2020-01-01T00:00:00Z")]
        #expect(policy.retainedFiles(in: snapshots, preserving: current, now: date("2026-09-28T12:00:00Z")) ==
                Set(["current", "future", "other-library"]))
    }

    @Test func equalTimestampsHaveDeterministicSelection() {
        let policy = BackupRetentionPolicy(recentSnapshotCount: 2, hourlyWindowHours: 0, dailyWindowDays: 0, monthlyWindowMonths: 0)
        let snapshots = ["z", "a", "m"].map { snapshot($0, "2026-09-28T12:00:00Z") }
        let now = date("2026-09-28T12:30:00Z")
        let expected: Set<String> = ["a", "m"]
        #expect(policy.retainedFiles(in: snapshots, preserving: snapshots[1], now: now) == expected)
        #expect(policy.retainedFiles(in: Array(snapshots.reversed()), preserving: snapshots[1], now: now) == expected)
    }
}
