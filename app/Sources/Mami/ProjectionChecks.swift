import Foundation

func checkProjection(at root: URL) throws {
    let path = root.appendingPathComponent("projection-reader.sqlite")
    let db = try SQLDatabase(path)
    try db.execute("PRAGMA user_version=\(ProjectionReader.schemaVersion)")
    try db.execute("CREATE TABLE checkpoint(id INTEGER PRIMARY KEY,ready INTEGER)")
    try db.execute("INSERT INTO checkpoint VALUES(1,1)")
    try db.execute("CREATE TABLE files(asset TEXT PRIMARY KEY,path TEXT,captured TEXT,camera TEXT,shape TEXT,kind TEXT,arrival INTEGER,summary TEXT)")
    try db.execute("CREATE TABLE vector_events(sequence INTEGER PRIMARY KEY)")
    try db.execute("INSERT INTO vector_events VALUES(250)")
    try db.execute("CREATE TABLE frames(asset TEXT,ordinal INTEGER,frame TEXT,timestamp REAL,crop TEXT)")
    let encoder = JSONEncoder()
    for ordinal in 1...250 {
        let asset = String(format: "asset-%03d", ordinal)
        let sample = Sample(path: asset, kind: "video", timestamp: 0, frame: "/offline/\(asset).jpg", score: nil, evidence: nil)
        let media = Media(path: asset, kind: "video", url: URL(fileURLWithPath: "/offline/\(asset).mp4"), frames: [], match: sample, metadata: nil, assetID: asset)
        try db.execute("INSERT INTO files VALUES(?,?,?,?,?,?,?,?)", [asset, asset, ordinal == 250 ? "" : "20260928120000", ordinal % 2 == 0 ? "A" : "B", "vertical", "video", String(ordinal), String(decoding: try encoder.encode(media), as: UTF8.self)])
    }
    try db.execute("INSERT INTO frames VALUES('asset-001',0,'/offline/frame.jpg',0.5,'[0,0,100,200]')")
    let reader = ProjectionReader(database: path)
    func require(_ condition: Bool, _ message: String) throws {
        if !condition { throw AppError.message(message) }
    }
    let first = try reader.page(scope: .init())
    let second = try reader.page(scope: .init(), after: first.cursor)
    let third = try reader.page(scope: .init(), after: second.cursor)
    let ids = (first.items + second.items + third.items).map(\.assetID)
    try require(first.total == 250 && first.items.count == ProjectionReader.pageSize, "Initial projection page decoded the wrong number of items")
    try require(ids.count == 250 && Set(ids).count == 250, "Keyset pages repeated or omitted media")
    try require(third.items.last?.assetID == "asset-250", "Unknown capture dates must sort last")
    var scope = ProjectionReader.Scope(camera: "A", assets: ["asset-002", "asset-200", "asset-249"])
    try require(try reader.page(scope: scope).items.map(\.assetID) == ["asset-002", "asset-200"], "Camera/manual filters must precede page limits")
    scope = ProjectionReader.Scope(arrivalThrough: 100)
    try require(try reader.page(scope: scope).total == 100, "Grid-lock arrival cutoff changed")
    try require(try reader.arrivals(after: 100) == 150, "New-arrival count is incorrect")
    try require(try reader.frames(asset: "asset-001").first?.timestamp == 0.5, "On-demand frame lookup failed")
    try require(try reader.frames(asset: "asset-001").first?.crop == [0,0,100,200], "Packed preview crop was lost")
    try require(try reader.media(paths: ["asset-249"]).first?.assetID == "asset-249", "Off-page search result lookup failed")
    let walPath = root.appendingPathComponent("clean-wal-projection.sqlite")
    do {
        let copy = try SQLDatabase(walPath)
        try db.backup(to: copy)
        try copy.execute("PRAGMA journal_mode=WAL")
        try copy.execute("UPDATE checkpoint SET ready=ready")
        try copy.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    }
    for suffix in ["-wal", "-shm"] {
        let sidecar = URL(fileURLWithPath: walPath.path + suffix)
        if FileManager.default.fileExists(atPath: sidecar.path) { try FileManager.default.removeItem(at: sidecar) }
    }
    try require(try ProjectionReader(database: walPath).facets().total == 250,
                "Read-only projection could not initialize clean WAL sidecars")
    print("PROJECTION TEST PASSED: keyset pages, scopes, grid-lock arrivals, offline frame lookup and off-page results")
}
