import Foundation

/// A single change signal for import, preview and inference publishers.
/// Not observable: views refresh through `changed(after:)` at a throttled rate,
/// so a burst of worker events causes one grid refresh instead of one per event.
@MainActor final class CatalogUpdates {
    static let shared = CatalogUpdates()
    /// Collects a burst of changes into one refresh; also the maximum refresh rate.
    static let refreshInterval: Duration = .milliseconds(500)
    private(set) var generation = 0
    private var waiters: [CheckedContinuation<Void, Never>] = []

    func notify() {
        generation += 1
        let resumed = waiters
        waiters = []
        for waiter in resumed { waiter.resume() }
    }

    /// Returns once `generation` differs from `seen`.
    func changed(after seen: Int) async {
        guard generation == seen else { return }
        await withCheckedContinuation { waiters.append($0) }
    }
}
