"""Forget originals the user moved to the Trash, and keep imports from restoring them.

The app moves each original to the Trash first; this records the deletion in the
personal store and removes generated catalog/index state. Search, vectors,
preview packs and faces follow through their existing change triggers/sync.
"""
import argparse
import contextlib
import json
import os
from pathlib import Path
import re
import sqlite3
from datetime import datetime, timezone

from index_store import connection as catalog_connection, ensure_schema
from user_store import connection as user_connection

ASSET_PATTERN = re.compile(r'sha256:[0-9a-f]{64}')
IMPORT_JOURNAL = Path('.mami-imports') / 'journal.sqlite'
# Jobs first, so frame-unit delete triggers find no job to requeue preview packing for.
GENERATED_TABLES = ('index_jobs', 'preview_jobs', 'index_units', 'scan_files', 'media')


def ensure_deleted_table(db):
    db.execute('CREATE TABLE IF NOT EXISTS deleted_media(asset TEXT PRIMARY KEY, paths TEXT NOT NULL, deleted TEXT NOT NULL)')
    for operation in ('INSERT', 'UPDATE'):
        db.execute(f'CREATE TRIGGER IF NOT EXISTS deleted_media_{operation} AFTER {operation} ON deleted_media BEGIN '
                   'UPDATE state SET revision=revision+1,change_token=lower(hex(randomblob(16))) WHERE id=1; END')


def deleted(catalog, digest):
    """Whether the user deleted this content and it is still gone; imports must not copy it back.

    Put Back from the Trash, or a deletion rolled back after a failure, leaves the
    content indexed again, so its record no longer blocks imports.
    """
    asset = 'sha256:' + digest
    with user_connection(catalog) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='deleted_media'").fetchone():
            return False
        if db.execute('SELECT 1 FROM deleted_media WHERE asset=?', (asset,)).fetchone() is None:
            return False
    with contextlib.closing(sqlite3.connect(Path(catalog).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='index_jobs'").fetchone():
            return True
        return db.execute('SELECT 1 FROM index_jobs WHERE asset=?', (asset,)).fetchone() is None


def media_paths(catalog, assets):
    with contextlib.closing(sqlite3.connect(Path(catalog).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        return {asset: [row[0] for row in db.execute('SELECT path FROM media WHERE asset=? ORDER BY path', (asset,))]
                for asset in assets}


def staging_links(original):
    """Import journals that published this original from a hidden staging link."""
    for folder in Path(original).parents:
        journal = folder / IMPORT_JOURNAL
        if journal.is_file():
            with contextlib.closing(sqlite3.connect(journal.resolve().as_uri() + '?mode=ro', uri=True)) as db:
                yield from (Path(row[0]) for row in db.execute(
                    'SELECT part FROM files WHERE destination=? AND part IS NOT NULL', (str(original),)))


def release_staging(trashed):
    """Unlink staging links that are the same file as a trashed original.

    The Trash keeps the bytes recoverable; without this, emptying it frees nothing.
    """
    released, warnings = 0, []
    for original, location in trashed.items():
        try:
            identity = os.stat(location)
            for part in staging_links(original):
                try:
                    found = os.stat(part)
                except FileNotFoundError:
                    continue
                if (found.st_dev, found.st_ino) == (identity.st_dev, identity.st_ino):
                    part.unlink()
                    released += 1
        except (OSError, sqlite3.Error) as error:
            warnings.append(f'{original}: {error}')
    return released, warnings


def forget(catalog, assets, trashed=None, now=None):
    assets = sorted(set(assets))
    if not assets or any(not ASSET_PATTERN.fullmatch(asset) for asset in assets):
        raise ValueError('Expected content identities (sha256:<digest>)')
    trashed = dict(trashed or {})
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    paths = media_paths(catalog, assets)
    # Personal intent first: if catalog cleanup fails, the records remain and the
    # recorded deletion only stops imports from copying the content back.
    with user_connection(catalog, write=True) as db:
        ensure_deleted_table(db)
        for asset in assets:
            db.execute('INSERT INTO deleted_media VALUES(?,?,?) ON CONFLICT(asset) DO UPDATE SET paths=excluded.paths,deleted=excluded.deleted',
                       (asset, json.dumps(paths[asset]), stamp))
    removed = {}
    with catalog_connection(catalog) as db:
        ensure_schema(db)
        for table in GENERATED_TABLES:
            removed[table] = sum(db.execute(f'DELETE FROM {table} WHERE asset=?', (asset,)).rowcount for asset in assets)
    released, warnings = release_staging(trashed)
    return dict(assets=len(assets), removed=removed, staging_released=released, warnings=warnings)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', type=Path, required=True)
    parser.add_argument('--asset', action='append', required=True)
    parser.add_argument('--trashed', action='append', nargs=2, default=[], metavar=('ORIGINAL', 'TRASH_LOCATION'))
    args = parser.parse_args()
    print(json.dumps(forget(args.catalog, args.asset, dict(args.trashed))), flush=True)


if __name__ == '__main__':
    main()
