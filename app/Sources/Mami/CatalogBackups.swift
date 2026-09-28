import Foundation
import MamiCore

/// Coalesces backup requests; persistence and retention live in SnapshotStore.
@MainActor final class CatalogBackups: ObservableObject {
    static let shared = CatalogBackups()
    @Published private(set) var status = "Personal-data backup pending"
    @Published private(set) var error: String?

    private let clock = ContinuousClock()
    private var task: Task<Void, Never>?
    private var inFlight = false
    private var pending = false
    private var pendingUrgent = false
    private var generation = 0
    private var lastCompleted: ContinuousClock.Instant?

    func schedule(urgent: Bool = false) {
        if inFlight {
            pending = true
            pendingUrgent = pendingUrgent || urgent
            return
        }
        if task != nil && !urgent { return }
        task?.cancel()
        generation += 1
        let current = generation
        let delay = BackupSchedulePolicy.delay(urgent: urgent, elapsedSinceCompletion: lastCompleted?.duration(to: clock.now))
        task = Task {
            do {
                try await clock.sleep(for: delay)
                try Task.checkCancellation()
                inFlight = true
                let snapshot = try await Task.detached { try Catalog.standard.userSnapshotIfChanged() }.value
                lastCompleted = clock.now
                status = "Personal data backed up · " + snapshot.created.formatted(date: .abbreviated, time: .shortened)
                error = nil
            } catch is CancellationError {
                // A newer scheduled request owns the state when generations differ.
            } catch {
                self.error = "Personal-data backup failed: \(error.localizedDescription)"
            }
            guard generation == current else { return }
            inFlight = false
            task = nil
            let again = pending
            let urgentAgain = pendingUrgent
            pending = false
            pendingUrgent = false
            if again { schedule(urgent: urgentAgain) }
        }
    }

    /// Orderly termination flushes personal changes under the catalog's writer lock.
    func flush() {
        task?.cancel()
        generation += 1
        task = nil
        pending = false
        pendingUrgent = false
        do {
            _ = try Catalog.standard.userSnapshotIfChanged()
            error = nil
        } catch {
            self.error = "Personal-data backup failed: \(error.localizedDescription)"
        }
        inFlight = false
    }
}
