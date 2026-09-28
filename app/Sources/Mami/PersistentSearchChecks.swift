import Foundation

@MainActor func checkPersistentSearch(at directory: URL) async throws {
    guard ProcessInfo.processInfo.environment["MAMI_CATALOG"] != nil else {
        throw AppError.message("Persistent search checks require an isolated MAMI_CATALOG")
    }
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
    let library = Library()
    let clock = ContinuousClock()
    let started = clock.now
    await library.load()
    guard library.ready else { throw AppError.message(library.error ?? "Library did not become ready") }
    let readyDuration = started.duration(to: clock.now).components
    let readySeconds = Double(readyDuration.seconds) + Double(readyDuration.attoseconds)/1e18
    let loadedCount = library.items.count
    var results: [[String: Any]] = []
    for query in ["milking goats", "bringing food to goats", "dunare"] {
        let before = clock.now
        library.query = query
        await library.search()?.value
        guard library.error == nil, library.showingMatches, !library.items.isEmpty else {
            await library.worker.stop()
            throw AppError.message(library.error ?? "Search returned no visible media")
        }
        let elapsed = before.duration(to: clock.now).components
        results.append(["query": query, "seconds": Double(elapsed.seconds)+Double(elapsed.attoseconds)/1e18,
                        "hits": library.items.count, "status": library.status])
    }
    await library.worker.stop()
    let report: [String: Any] = ["ready_seconds": readySeconds, "loaded_items": loadedCount, "queries": results,
                               "scope": "Native Library load and search, isolated catalog, warm OS cache possible; not a reboot measurement."]
    let data = try JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys])
    try data.write(to: directory.appendingPathComponent("result.json"), options: .withoutOverwriting)
    print(String(decoding: data, as: UTF8.self))
}
