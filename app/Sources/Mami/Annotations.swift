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
    let directory: URL
    let catalog: Catalog

    init(directory: URL = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("mami-lab/catalog/annotations")) {
        self.directory = directory
        let standard = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("mami-lab/catalog/annotations")
        self.catalog = directory == standard ? .standard : Catalog(directory: directory.appendingPathComponent("database"))
    }

    func value(for media: Media) -> Annotation { values[media.assetID] ?? Annotation() }

    nonisolated static func read(_ directory: URL) throws -> [String: Annotation] {
        let standard = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("mami-lab/catalog/annotations")
        let catalog: Catalog = directory == standard ? .standard : Catalog(directory: directory.appendingPathComponent("database"))
        try catalog.migrateAnnotations(from: directory, identities: ContentIdentity.paths)
        return try catalog.annotations()
    }

    func load() async {
        guard !ready else { return }
        do {
            let directory = directory
            values = try await Task.detached { try Self.read(directory) }.value
            ready = true
            if catalog.directory == Catalog.standard.directory { CatalogBackups.shared.schedule() }
        } catch { self.error = "Could not load saved tags: \(error.localizedDescription)" }
    }

    func save(_ value: Annotation, for media: Media) {
        guard ready else { error = "Saved metadata is still loading."; return }
        guard values[media.assetID] != value else { return }
        do {
            try catalog.save(value, asset: media.assetID)
            values[media.assetID] = value
            error = nil
            if catalog.directory == Catalog.standard.directory { CatalogBackups.shared.schedule() }
        } catch { self.error = "Could not save metadata: \(error.localizedDescription)" }
    }

    func toggleFavorite(_ media: Media) {
        var value = value(for: media)
        value.favorite.toggle()
        save(value, for: media)
    }
}
