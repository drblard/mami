"""Access migrated personal data using the same writer lock as the media catalog."""
import contextlib
import fcntl
from pathlib import Path
import sqlite3
import json
from app_paths import personal_database

# Every personal table; must match PersonalDataMigration.tables (Swift), checked by tests.
PERSONAL_TABLES = ('annotations', 'annotation_history', 'imported_events', 'clip_selection', 'photos_import_history',
                   'media_roots', 'people', 'face_labels', 'people_history', 'deleted_media')


@contextlib.contextmanager
def connection(catalog, write=False):
    catalog = Path(catalog)
    with (catalog.parent/'catalog.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = personal_database(catalog)
        migrated = path.exists()
        marker=path.parent/'ownership.json'
        if not migrated and marker.exists():
            raise ValueError('Personal database is missing; restore its backup before opening the library')
        if not migrated:
            path = catalog  # Existing standalone fixtures / pre-migration tools.
        uri = path.resolve().as_uri() + ('?mode=rw' if write else '?mode=ro')
        with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=5)) as db:
            if not migrated and db.execute("SELECT 1 FROM sqlite_master WHERE name='user_store_migration'").fetchone():
                raise ValueError('Personal database is missing; restore its backup instead of using stale legacy data')
            if migrated:
                row = db.execute('SELECT version,source_identity FROM user_store_info').fetchone()
                with contextlib.closing(sqlite3.connect(catalog.resolve().as_uri()+'?mode=ro', uri=True)) as source:
                    identity = source.execute('SELECT identity FROM state WHERE id=1').fetchone()[0]
                if row != (1, identity):
                    raise ValueError('Personal data belongs to another catalog or unsupported schema')
                if marker.exists():
                    ownership=json.loads(marker.read_text())
                    if ownership.get('version')!=1 or ownership.get('identity')!=identity:
                        raise ValueError('Personal-data ownership marker belongs to another library')
            if write:
                db.execute('PRAGMA journal_mode=PERSIST')
                db.execute('PRAGMA synchronous=FULL')
                db.execute('PRAGMA fullfsync=ON')
                db.execute('BEGIN IMMEDIATE')
            else:
                db.execute('BEGIN')
            with db:
                yield db
