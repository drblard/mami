import Foundation

/// A detected face, identified by position so labels survive a face-index rebuild.
struct FaceRef: Hashable, Sendable, Codable {
    let asset: String
    let timestamp: Double?
    let box: [Double]
    init(asset: String, timestamp: Double?, box: [Double]) {
        let rounded = { (value: Double) in (value * 1_000_000).rounded() / 1_000_000 }
        self.asset = asset
        self.timestamp = timestamp.map(rounded)
        self.box = box.map(rounded)
    }
    var key: String {
        let time = timestamp.map { String(format: "%.6f", $0) } ?? "photo"
        return "\(asset)|\(time)|" + box.map { String(format: "%.6f", $0) }.joined(separator: ",")
    }
}

struct Person: Identifiable, Hashable, Sendable, Codable {
    let id: String
    var name: String
    let created: String
}

struct FaceLabel: Hashable, Sendable, Codable {
    enum Verdict: String, Sendable, Codable { case confirmed, rejected }
    let face: FaceRef
    let person: String
    let verdict: Verdict
}

/// Personal people edits. Each returns the exact prior rows for its scope, so
/// undo restores that scope without touching unrelated labels.
enum PeopleChange: Sendable {
    case create(Person)
    case rename(String, to: String)
    case confirm([FaceRef], as: String)
    case reject([FaceRef], from: String)
    case merge(String, into: String)
    case delete(String)

    var action: String {
        switch self {
        case .create: return "create"
        case .rename: return "rename"
        case .confirm: return "confirm"
        case .reject: return "reject"
        case .merge: return "merge"
        case .delete: return "delete"
        }
    }
    fileprivate var scope: (faces: Set<FaceRef>, people: Set<String>) {
        switch self {
        case .create(let person): return ([], [person.id])
        case .rename(let id, _), .delete(let id): return ([], [id])
        // Confirming/rejecting only changes labels on these faces, including other people's.
        case .confirm(let faces, _), .reject(let faces, _): return (Set(faces), [])
        case .merge(let source, let target): return ([], [source, target])
        }
    }
}

struct PeopleSnapshot: Sendable {
    let faces: Set<FaceRef>
    let personIDs: Set<String>
    let people: [Person]
    let labels: [FaceLabel]
}

extension Catalog {
    fileprivate static let bumpRevision = "UPDATE state SET revision=revision+1,change_token=lower(hex(randomblob(16))) WHERE id=1"

    fileprivate func peopleTables(_ db: SQLDatabase) throws {
        guard try !db.hasTable("face_labels") else { return }
        try db.transaction {
            try db.execute("CREATE TABLE IF NOT EXISTS people(id TEXT PRIMARY KEY, name TEXT NOT NULL, created TEXT NOT NULL)")
            try db.execute("""
                CREATE TABLE face_labels(face_key TEXT NOT NULL, asset TEXT NOT NULL, timestamp REAL,
                    x1 REAL NOT NULL, y1 REAL NOT NULL, x2 REAL NOT NULL, y2 REAL NOT NULL, person TEXT NOT NULL,
                    verdict TEXT NOT NULL CHECK(verdict IN ('confirmed','rejected')), updated TEXT NOT NULL,
                    PRIMARY KEY(face_key, person))
                """)
            try db.execute("CREATE INDEX face_labels_person ON face_labels(person)")
            try db.execute("CREATE TABLE people_history(id INTEGER PRIMARY KEY, action TEXT NOT NULL, payload TEXT NOT NULL, updated TEXT NOT NULL)")
            for table in ["people", "face_labels"] {
                for operation in ["INSERT", "UPDATE", "DELETE"] {
                    try db.execute("CREATE TRIGGER \(table)_\(operation) AFTER \(operation) ON \(table) BEGIN \(Self.bumpRevision); END")
                }
            }
            try db.execute(Self.bumpRevision)
        }
    }

    func people() throws -> [Person] {
        try userAccess { db in
            try peopleTables(db)
            return try db.rows("SELECT id,name,created FROM people ORDER BY name COLLATE NOCASE, id").map { Person(id: $0[0], name: $0[1], created: $0[2]) }
        }
    }

    fileprivate func snapshot(_ db: SQLDatabase, faces: Set<FaceRef>, people ids: Set<String>) throws -> PeopleSnapshot {
        var people: [Person] = [], labels = Set<FaceLabel>()
        for id in ids.sorted() {
            people += try db.rows("SELECT id,name,created FROM people WHERE id=?", [id]).map { Person(id: $0[0], name: $0[1], created: $0[2]) }
        }
        func read(_ rows: [[String]]) {
            for row in rows {
                let face = FaceRef(asset: row[0], timestamp: row[1].isEmpty ? nil : Double(row[1]), box: row[2...5].compactMap(Double.init))
                if let verdict = FaceLabel.Verdict(rawValue: row[7]) { labels.insert(FaceLabel(face: face, person: row[6], verdict: verdict)) }
            }
        }
        let columns = "SELECT asset,timestamp,x1,y1,x2,y2,person,verdict FROM face_labels"
        for face in faces { read(try db.rows(columns + " WHERE face_key=?", [face.key])) }
        for id in ids { read(try db.rows(columns + " WHERE person=?", [id])) }
        return PeopleSnapshot(faces: faces, personIDs: ids, people: people, labels: labels.sorted { ($0.face.key, $0.person) < ($1.face.key, $1.person) })
    }

    private func insert(_ label: FaceLabel, _ db: SQLDatabase, updated: String) throws {
        let face = label.face
        try db.execute("INSERT OR REPLACE INTO face_labels VALUES(?,?,NULLIF(?,''),?,?,?,?,?,?,?)",
                       [face.key, face.asset, face.timestamp.map { String($0) } ?? ""] + face.box.map { String($0) } + [label.person, label.verdict.rawValue, updated])
    }

    /// Applies one edit atomically and returns the snapshot that undoes it.
    @discardableResult func apply(_ change: PeopleChange, now: Date = Date()) throws -> PeopleSnapshot {
        try userAccess { db in
            try peopleTables(db)
            let updated = ISO8601DateFormatter().string(from: now)
            var undo: PeopleSnapshot!
            try db.transaction {
                let scope = change.scope
                undo = try snapshot(db, faces: scope.faces, people: scope.people)
                switch change {
                case .create(let person):
                    try db.execute("INSERT INTO people VALUES(?,?,?)", [person.id, person.name, person.created])
                case .rename(let id, let name):
                    try db.execute("UPDATE people SET name=? WHERE id=?", [name, id])
                case .confirm(let faces, let person):
                    for face in faces {
                        // A face shows one person: confirming replaces other confirmations.
                        try db.execute("DELETE FROM face_labels WHERE face_key=? AND (verdict='confirmed' OR person=?)", [face.key, person])
                        try insert(FaceLabel(face: face, person: person, verdict: .confirmed), db, updated: updated)
                    }
                case .reject(let faces, let person):
                    for face in faces { try insert(FaceLabel(face: face, person: person, verdict: .rejected), db, updated: updated) }
                case .merge(let source, let target):
                    let moved = undo.labels.filter { $0.person == source }
                    try db.execute("DELETE FROM face_labels WHERE person=?", [source])
                    for label in moved {
                        try insert(FaceLabel(face: label.face, person: target, verdict: label.verdict), db, updated: updated)
                    }
                    try db.execute("DELETE FROM people WHERE id=?", [source])
                case .delete(let id):
                    try db.execute("DELETE FROM face_labels WHERE person=?", [id])
                    try db.execute("DELETE FROM people WHERE id=?", [id])
                }
                try history(change.action, undo, db, updated: updated)
            }
            return undo
        }
    }

    /// Restores a scope exactly as captured by `apply`.
    func restore(_ snapshot: PeopleSnapshot, now: Date = Date()) throws {
        try userAccess { db in
            try peopleTables(db)
            let updated = ISO8601DateFormatter().string(from: now)
            try db.transaction {
                for face in snapshot.faces { try db.execute("DELETE FROM face_labels WHERE face_key=?", [face.key]) }
                for id in snapshot.personIDs {
                    try db.execute("DELETE FROM face_labels WHERE person=?", [id])
                    try db.execute("DELETE FROM people WHERE id=?", [id])
                }
                for person in snapshot.people { try db.execute("INSERT INTO people VALUES(?,?,?)", [person.id, person.name, person.created]) }
                for label in snapshot.labels { try insert(label, db, updated: updated) }
                try history("undo", snapshot, db, updated: updated)
            }
        }
    }

    private func history(_ action: String, _ previous: PeopleSnapshot, _ db: SQLDatabase, updated: String) throws {
        struct Entry: Encodable { let people: [Person]; let labels: [FaceLabel] }
        let payload = String(decoding: try JSONEncoder().encode(Entry(people: previous.people, labels: previous.labels)), as: UTF8.self)
        try db.execute("INSERT INTO people_history(action,payload,updated) VALUES(?,?,?)", [action, payload, updated])
    }
}
