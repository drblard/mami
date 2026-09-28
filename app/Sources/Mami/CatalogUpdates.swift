import Foundation

/// A single change signal for import, preview and inference publishers.
@MainActor final class CatalogUpdates: ObservableObject {
    static let shared = CatalogUpdates()
    @Published private(set) var generation = 0
    func notify() { generation += 1 }
}
