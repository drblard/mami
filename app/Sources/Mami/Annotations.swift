import Foundation

struct Annotation: Codable, Sendable, Equatable {
    var favorite = false
    var tags: [String] = []
    var place = ""
}

struct AnnotationEvent: Codable {
    let asset: String
    let updated: Date
    let value: Annotation
}

enum ContentIdentity {
    static let locations: [String: String] = {
        guard let url = Bundle.main.resourceURL?.appendingPathComponent("relocations.json"),
              let data = try? Data(contentsOf: url),
              let locations = try? JSONDecoder().decode([String: String].self, from: data) else { return [:] }
        return locations
    }()
    static let paths: [String: String] = {
        guard let url = Bundle.main.resourceURL?.appendingPathComponent("identities.json"),
              let data = try? Data(contentsOf: url),
              let paths = try? JSONDecoder().decode([String: String].self, from: data) else { return [:] }
        return paths
    }()
}

/// Transactional current labels plus append-only history in the SQLite catalog.
/// Earlier JSON events are imported once and retained at their original paths.
@MainActor final class Annotations: ObservableObject {
    @Published private(set) var values: [String: Annotation] = [:]
    @Published var error: String?
    @Published private(set) var ready = false
    var directory: URL { catalog.legacyAnnotationsDirectory }
    let catalog: Catalog

    init(catalog: Catalog = .standard) {
        self.catalog = catalog
    }

    convenience init(directory: URL) {
        self.init(catalog: Catalog(directory: directory.appendingPathComponent("database"), legacyAnnotations: directory))
    }

    func value(for media: Media) -> Annotation { values[media.assetID] ?? Annotation() }

    nonisolated static func read(_ directory: URL) throws -> [String: Annotation] {
        try read(catalog: Catalog(directory: directory.appendingPathComponent("database"), legacyAnnotations: directory))
    }

    nonisolated static func read(catalog: Catalog) throws -> [String: Annotation] {
        try catalog.migrateAnnotations(from: catalog.legacyAnnotationsDirectory, identities: ContentIdentity.paths)
        return try catalog.annotations()
    }

    func load() async {
        guard !ready else { return }
        do {
            let catalog = catalog
            values = try await Task.detached { try Self.read(catalog: catalog) }.value
            ready = true
            if catalog.directory == Catalog.standard.directory { CatalogBackups.shared.schedule(urgent: true) }
        } catch { self.error = "Could not load saved tags: \(error.localizedDescription)" }
    }

    /// Shows the edit at once, then saves off the main thread, in order: the catalog
    /// lock can be held by a worker transaction. A failed save is undone and reported.
    func save(_ value: Annotation, for media: Media) {
        guard ready else { error = "Saved metadata is still loading."; return }
        let asset = media.assetID
        guard values[asset] != value else { return }
        let previous = values[asset]
        values[asset] = value
        error = nil
        let catalog = catalog, prior = writes
        writes = Task {
            await prior?.value
            do {
                try await Task.detached(priority: .userInitiated) { try catalog.save(value, asset: asset) }.value
                if catalog.directory == Catalog.standard.directory { CatalogBackups.shared.schedule(urgent: true) }
            } catch {
                if values[asset] == value { values[asset] = previous }
                self.error = "Could not save metadata: \(error.localizedDescription)"
            }
        }
    }
    private var writes: Task<Void, Never>?
    /// Waits for queued saves (integration checks read the store afterwards).
    func flush() async { await writes?.value }

    func toggleFavorite(_ media: Media) {
        var value = value(for: media)
        value.favorite.toggle()
        save(value, for: media)
    }
}
