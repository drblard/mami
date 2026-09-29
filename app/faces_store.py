"""Generated face index: jobs, detected faces, embeddings, groups and suggestions.

Everything here can be rebuilt from originals and the pinned model. The user's
people and confirmations live in the personal store and are only read here.
"""
import contextlib
import fcntl
from pathlib import Path
import sqlite3

import numpy as np

FACES_SCHEMA_VERSION = 1
MAX_FACE_JOB_ATTEMPTS = 3
PENDING_FACE_PREDICATE = f"state='queued' OR state='running' OR (state='error' AND attempts<{MAX_FACE_JOB_ATTEMPTS})"
EMBEDDING_DTYPE = np.float32
# Labels are matched to regenerated faces by position; detections from another
# execution provider differ slightly, so exact coordinates cannot be the key.
LABEL_MATCH_OVERLAP = 0.5
LABEL_MATCH_SECONDS = 0.25


@contextlib.contextmanager
def connection(path, write=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if write:
        with (path.parent / 'faces.lock').open('a+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with contextlib.closing(sqlite3.connect(path, timeout=10)) as db:
                db.row_factory = sqlite3.Row
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('PRAGMA synchronous=NORMAL')
                db.execute('PRAGMA foreign_keys=ON')
                db.execute('BEGIN IMMEDIATE')
                with db:
                    ensure_schema(db)
                    yield db
    else:
        with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            db.execute('BEGIN')
            yield db


def ensure_schema(db):
    present = db.execute("SELECT 1 FROM sqlite_master WHERE name='faces_schema'").fetchone()
    version = db.execute('SELECT version FROM faces_schema').fetchone()[0] if present else 0
    if version == FACES_SCHEMA_VERSION:
        return
    if version != 0:
        raise ValueError('Unsupported face index version; rebuild it into a new directory')
    db.executescript(f'''
        CREATE TABLE faces_schema(version INTEGER NOT NULL);
        INSERT INTO faces_schema VALUES({FACES_SCHEMA_VERSION});
        CREATE TABLE face_state(id INTEGER PRIMARY KEY CHECK(id=1), change_token TEXT NOT NULL,
            paused INTEGER NOT NULL DEFAULT 0, grouped TEXT NOT NULL DEFAULT '');
        INSERT INTO face_state(id,change_token) VALUES(1,lower(hex(randomblob(16))));
        CREATE TABLE face_jobs(asset TEXT PRIMARY KEY, path TEXT NOT NULL, kind TEXT NOT NULL,
            signature TEXT NOT NULL, pipeline TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
            error TEXT, attempts INTEGER NOT NULL DEFAULT 0, capture_time REAL);
        CREATE INDEX face_jobs_pending ON face_jobs(capture_time DESC, asset) WHERE {PENDING_FACE_PREDICATE};
        CREATE TABLE faces(id INTEGER PRIMARY KEY AUTOINCREMENT, asset TEXT NOT NULL, timestamp REAL,
            x1 REAL NOT NULL, y1 REAL NOT NULL, x2 REAL NOT NULL, y2 REAL NOT NULL,
            score REAL NOT NULL, eye_distance REAL NOT NULL, frontalness REAL NOT NULL,
            sharpness REAL NOT NULL, norm REAL NOT NULL, track INTEGER NOT NULL,
            representative INTEGER NOT NULL, reliable INTEGER NOT NULL, crop TEXT, embedding BLOB NOT NULL);
        CREATE INDEX faces_asset ON faces(asset);
        CREATE INDEX faces_representatives ON faces(id) WHERE representative=1 AND reliable=1;
        CREATE TABLE face_groups(face INTEGER PRIMARY KEY REFERENCES faces(id) ON DELETE CASCADE, grp INTEGER NOT NULL);
        CREATE INDEX face_groups_group ON face_groups(grp);
        CREATE TABLE face_assignments(face INTEGER PRIMARY KEY REFERENCES faces(id) ON DELETE CASCADE,
            person TEXT NOT NULL, source TEXT NOT NULL CHECK(source IN ('confirmed','suggested','track')),
            similarity REAL);
        CREATE INDEX face_assignments_person ON face_assignments(person, source);
    ''')
    for table in ('face_jobs', 'faces', 'face_groups', 'face_assignments'):
        for operation in ('INSERT', 'UPDATE', 'DELETE'):
            db.execute(f"CREATE TRIGGER {table}_{operation} AFTER {operation} ON {table} "
                       "BEGIN UPDATE face_state SET change_token=lower(hex(randomblob(16))) WHERE id=1; END")


def sync_jobs(db, catalog_rows, pipeline):
    """Mirror catalog assets whose previews are complete; requeue changed originals.

    catalog_rows: iterable of (asset, path, kind, signature, capture_time).
    Returns (added, requeued, removed) counts.
    """
    current = {row['asset']: row for row in db.execute('SELECT asset,path,signature,pipeline FROM face_jobs')}
    seen = set()
    added = requeued = 0
    for asset, path, kind, signature, capture_time in catalog_rows:
        seen.add(asset)
        existing = current.get(asset)
        if existing is None:
            db.execute('INSERT INTO face_jobs(asset,path,kind,signature,pipeline,capture_time) VALUES(?,?,?,?,?,?)',
                       (asset, path, kind, signature, pipeline, capture_time))
            added += 1
        elif existing['signature'] != signature or existing['pipeline'] != pipeline:
            db.execute('DELETE FROM faces WHERE asset=?', (asset,))
            db.execute("UPDATE face_jobs SET path=?,kind=?,signature=?,pipeline=?,state='queued',attempts=0,error=NULL,capture_time=? WHERE asset=?",
                       (path, kind, signature, pipeline, capture_time, asset))
            requeued += 1
        elif existing['path'] != path:
            db.execute('UPDATE face_jobs SET path=? WHERE asset=?', (path, asset))
    removed = [asset for asset in current if asset not in seen]
    for asset in removed:
        db.execute('DELETE FROM faces WHERE asset=?', (asset,))
        db.execute('DELETE FROM face_jobs WHERE asset=?', (asset,))
    return added, requeued, len(removed)


def next_job(db):
    return db.execute(f'SELECT * FROM face_jobs WHERE {PENDING_FACE_PREDICATE} ORDER BY capture_time DESC, asset LIMIT 1').fetchone()


def counts(db):
    rows = dict(db.execute('SELECT state,count(*) FROM face_jobs GROUP BY state').fetchall())
    total = sum(rows.values())
    return dict(remaining=total - rows.get('complete', 0), completed=rows.get('complete', 0), failed=rows.get('error', 0))


def publish_faces(db, asset, signature, faces):
    """Replace one asset's faces atomically, only if its job still has this signature."""
    job = db.execute('SELECT signature FROM face_jobs WHERE asset=?', (asset,)).fetchone()
    if job is None or job['signature'] != signature:
        return False
    db.execute('DELETE FROM faces WHERE asset=?', (asset,))
    for face in faces:
        embedding = np.asarray(face['embedding'], dtype=EMBEDDING_DTYPE)
        db.execute('INSERT INTO faces(asset,timestamp,x1,y1,x2,y2,score,eye_distance,frontalness,sharpness,norm,track,representative,reliable,crop,embedding) '
                   'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (asset, face['timestamp'], *face['box'], face['score'], face['eye_distance'], face['frontalness'],
                    face['sharpness'], face['norm'], face['track'], int(face['representative']), int(face['reliable']),
                    face.get('crop'), embedding.tobytes()))
    db.execute("UPDATE face_jobs SET state='complete',error=NULL WHERE asset=?", (asset,))
    return True


def fail_job(db, asset, error):
    db.execute("UPDATE face_jobs SET state='error',attempts=attempts+1,error=? WHERE asset=?", (str(error)[-1000:], asset))


def embeddings(db, where='representative=1 AND reliable=1'):
    rows = db.execute(f'SELECT id,embedding FROM faces WHERE {where} ORDER BY id').fetchall()
    ids = np.array([row['id'] for row in rows], dtype=np.int64)
    matrix = np.frombuffer(b''.join(row['embedding'] for row in rows), dtype=EMBEDDING_DTYPE)
    return ids, matrix.reshape(len(rows), -1) if len(rows) else np.zeros((0, 512), EMBEDDING_DTYPE)


def overlap(a, b):
    width = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    height = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    intersection = width * height
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - intersection
    return intersection / union if union > 0 else 0.0


def match_label(db, asset, timestamp, box):
    """Face id for a stored personal label, or None if that face no longer exists."""
    if timestamp is None:
        rows = db.execute('SELECT id,x1,y1,x2,y2 FROM faces WHERE asset=? AND timestamp IS NULL', (asset,)).fetchall()
    else:
        rows = db.execute('SELECT id,x1,y1,x2,y2 FROM faces WHERE asset=? AND abs(timestamp-?)<=?',
                          (asset, timestamp, LABEL_MATCH_SECONDS)).fetchall()
    best = max(((overlap(box, (row['x1'], row['y1'], row['x2'], row['y2'])), row['id']) for row in rows), default=(0.0, None))
    return best[1] if best[0] >= LABEL_MATCH_OVERLAP else None


def face_set(db):
    """Identifies the current set of faces; grouping is stale when it changes."""
    count, highest = db.execute('SELECT count(*),coalesce(max(id),0) FROM faces').fetchone()
    return f'{count}:{highest}'


def grouping_stale(db):
    return db.execute('SELECT grouped FROM face_state WHERE id=1').fetchone()[0] != face_set(db)
