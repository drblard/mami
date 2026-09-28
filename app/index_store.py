"""Durable generated-work queues and verified-import publication."""
import contextlib
from datetime import datetime
import fcntl
import json
from pathlib import Path
import sqlite3

INDEX_SCHEMA_VERSION = 2
MEDIA_EXTENSIONS = {'.mp4': 'video', '.mov': 'video', '.jpg': 'image', '.jpeg': 'image', '.png': 'image', '.heic': 'image'}
THUMBNAIL_STAGE = 0
COARSE_STAGE = 1
DENSE_STAGE = 2
COMPLETE_STAGE = 3


def signature(path):
    stat = Path(path).stat()
    return json.dumps([stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino, stat.st_dev])


@contextlib.contextmanager
def connection(database):
    database = Path(database)
    with (database.parent/'catalog.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with contextlib.closing(sqlite3.connect(database, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA journal_mode=PERSIST')
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            with db:
                yield db


def ensure_schema(db):
    present = db.execute("SELECT 1 FROM sqlite_master WHERE name='index_schema'").fetchone()
    version = db.execute('SELECT version FROM index_schema').fetchone()[0] if present else 0
    if version == INDEX_SCHEMA_VERSION:
        return
    if version not in (0, 1):
        raise ValueError('Unsupported indexing queue version')
    if version == 0:
        db.execute('CREATE TABLE index_schema(version INTEGER NOT NULL)')
        db.execute('INSERT INTO index_schema VALUES(1)')
        db.execute('CREATE TABLE scan_control(id INTEGER PRIMARY KEY, paused INTEGER NOT NULL)')
        db.execute('INSERT INTO scan_control VALUES(1,0)')
        db.execute('CREATE TABLE scan_files(path TEXT PRIMARY KEY, signature TEXT NOT NULL, asset TEXT NOT NULL)')
        db.execute('CREATE TABLE index_jobs(asset TEXT PRIMARY KEY,path TEXT NOT NULL,kind TEXT NOT NULL,signature TEXT NOT NULL,logical TEXT NOT NULL,state TEXT NOT NULL,error TEXT,attempts INTEGER NOT NULL DEFAULT 0)')
        db.execute('CREATE TABLE index_units(asset TEXT NOT NULL,pipeline TEXT NOT NULL,stage TEXT NOT NULL,ordinal INTEGER NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(asset,pipeline,stage,ordinal))')
        for table in ('scan_control', 'scan_files', 'index_jobs', 'index_units'):
            track_changes(db, table)
    if 'capture_time' not in {row[1] for row in db.execute('PRAGMA table_info(index_jobs)')}:
        db.execute('ALTER TABLE index_jobs ADD COLUMN capture_time REAL')
    db.execute('CREATE TABLE preview_control(id INTEGER PRIMARY KEY,paused INTEGER NOT NULL,dispatch_order INTEGER NOT NULL DEFAULT 0)')
    db.execute('INSERT INTO preview_control VALUES(1,0,0)')
    db.execute("CREATE TABLE preview_jobs(asset TEXT PRIMARY KEY,stage INTEGER NOT NULL DEFAULT 0,state TEXT NOT NULL DEFAULT 'queued',error TEXT,attempts INTEGER NOT NULL DEFAULT 0,priority INTEGER NOT NULL DEFAULT 0,last_served INTEGER NOT NULL DEFAULT 0,capture_time REAL)")
    db.execute("INSERT INTO preview_jobs(asset,stage,state,capture_time) SELECT asset,CASE WHEN state='complete' THEN ? ELSE ? END,CASE WHEN state='complete' THEN 'complete' ELSE 'queued' END,capture_time FROM index_jobs", (COMPLETE_STAGE, THUMBNAIL_STAGE))
    db.execute("CREATE INDEX preview_schedule ON preview_jobs(priority DESC,stage,last_served,capture_time DESC,asset) WHERE state IN ('queued','running') OR (state='error' AND attempts<3)")
    for table in ('preview_control', 'preview_jobs'):
        track_changes(db, table)
    db.execute("CREATE TRIGGER index_jobs_preview AFTER INSERT ON index_jobs BEGIN INSERT OR IGNORE INTO preview_jobs(asset,state,capture_time) VALUES(NEW.asset,'queued',NEW.capture_time); END")
    db.execute("CREATE TRIGGER index_jobs_preview_time AFTER UPDATE OF capture_time ON index_jobs BEGIN UPDATE preview_jobs SET capture_time=NEW.capture_time WHERE asset=NEW.asset AND capture_time IS NOT NEW.capture_time; END")
    db.execute('UPDATE index_schema SET version=?', (INDEX_SCHEMA_VERSION,))
    db.execute('UPDATE state SET revision=revision+1,change_token=lower(hex(randomblob(16))) WHERE id=1')


def track_changes(db, table):
    for operation in ('INSERT', 'UPDATE', 'DELETE'):
        db.execute(f'CREATE TRIGGER {table}_{operation} AFTER {operation} ON {table} BEGIN UPDATE state SET revision=revision+1,change_token=lower(hex(randomblob(16))) WHERE id=1; END')


def register_verified_import(database, path, digest, *, capture_time, source_device):
    """Admit an already verified original without hashing or probing it again."""
    path = Path(path)
    kind = MEDIA_EXTENSIONS.get(path.suffix.lower())
    if database is None or kind is None:
        return False
    if path.is_symlink() or not path.is_file():
        raise ValueError('Verified publication requires a regular original')
    current_signature = signature(path)
    asset = 'sha256:' + digest
    logical = 'library:' + asset
    captured = datetime.fromtimestamp(capture_time)
    placeholder = dict(path=logical, kind=kind, timestamp=0 if kind == 'video' else None, frame='')
    metadata = dict(date=captured.strftime('%d %b %Y'), sortDate=captured.strftime('%Y%m%d%H%M%S'),
                    details=[], tags=[], technical=[], duration=None, location=None)
    if source_device == 'iCloud':
        metadata['source'] = 'iCloud'
    media = dict(path=logical, kind=kind, url=path.as_uri(), frames=[], match=placeholder,
                 metadata=metadata, assetID=asset, previewState='pending')
    with connection(database) as db:
        ensure_schema(db)
        if signature(path) != current_signature:
            raise RuntimeError('Original changed before catalog publication')
        existing = db.execute('SELECT asset,payload FROM media WHERE path=?', (str(path),)).fetchone()
        known_asset = db.execute('SELECT path,payload FROM media WHERE asset=?', (asset,)).fetchone()
        job = db.execute('SELECT signature FROM index_jobs WHERE asset=?', (asset,)).fetchone()
        db.execute('INSERT INTO scan_files VALUES(?,?,?) ON CONFLICT(path) DO UPDATE SET signature=excluded.signature,asset=excluded.asset WHERE scan_files.signature!=excluded.signature OR scan_files.asset!=excluded.asset',
                   (str(path), current_signature, asset))
        changed = False
        if known_asset and known_asset['path'] != str(path):
            if Path(known_asset['path']).exists():
                db.execute('UPDATE preview_jobs SET priority=1 WHERE asset=? AND priority!=1', (asset,))
                return False  # One logical item per content identity, not duplicate grid IDs.
            restored = json.loads(known_asset['payload'])
            restored['url'] = path.as_uri()
            db.execute('UPDATE media SET path=?,payload=? WHERE path=?',
                       (str(path), json.dumps(restored, sort_keys=True), known_asset['path']))
            existing = dict(asset=asset, payload=json.dumps(restored))
            changed = True
        if job is None and not known_asset:
            db.execute("INSERT INTO index_jobs(asset,path,kind,signature,logical,state,capture_time) VALUES(?,?,?,?,?,'queued',?)",
                       (asset, str(path), kind, current_signature, logical, capture_time))
        elif job is not None:
            db.execute('UPDATE index_jobs SET path=?,signature=?,capture_time=? WHERE asset=? AND (path!=? OR signature!=? OR capture_time IS NULL)',
                       (str(path), current_signature, capture_time, asset, str(path), current_signature))
            if job['signature'] != current_signature:
                db.execute("UPDATE preview_jobs SET state='queued',attempts=0,error=NULL WHERE asset=? AND state!='complete'", (asset,))
        db.execute('UPDATE preview_jobs SET priority=1 WHERE asset=? AND priority!=1', (asset,))
        if existing is None or existing['asset'] != asset:
            # Preserve richer existing/seeded records. Registration never replaces previews.
            db.execute('INSERT INTO media VALUES(?,?,?) ON CONFLICT(path) DO UPDATE SET asset=excluded.asset,payload=excluded.payload',
                       (str(path), asset, json.dumps(media, sort_keys=True)))
            return True
    return changed
