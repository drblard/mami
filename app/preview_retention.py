"""Bounded cleanup of registered atlas generations; unknown/changed files stay put."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3

from index_store import connection

RETAINED_SUPERSEDED_PACKS = 1
CLEANUP_BATCH_SIZE = 8


def ensure_generation_schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS preview_generations(id INTEGER PRIMARY KEY,asset TEXT NOT NULL,directory TEXT UNIQUE NOT NULL,staging TEXT NOT NULL,digest TEXT NOT NULL,review TEXT)')
    db.execute('CREATE INDEX IF NOT EXISTS preview_generation_asset ON preview_generations(asset,id)')
    if 'obsolete' not in {row[1] for row in db.execute('PRAGMA table_info(preview_generations)')}:
        db.execute('ALTER TABLE preview_generations ADD COLUMN obsolete INTEGER NOT NULL DEFAULT 1')
        db.execute('UPDATE preview_generations SET obsolete=0 WHERE directory IN (SELECT directory FROM preview_packs)')
    db.execute('CREATE INDEX IF NOT EXISTS preview_generation_cleanup ON preview_generations(obsolete,review,id)')


def register_generation(database,asset,directory,staging):
    digest=hashlib.sha256((staging/'manifest.json').read_bytes()).hexdigest()
    with connection(database) as db:
        ensure_generation_schema(db)
        db.execute('INSERT INTO preview_generations(asset,directory,staging,digest) VALUES(?,?,?,?)',
                   (asset,str(directory),str(staging),digest))


def verified_directory(path,root,digest):
    if path.is_symlink() or path.parent.resolve()!=root:return False
    manifest_path=path/'manifest.json'
    if manifest_path.is_symlink() or not manifest_path.is_file():return False
    data=manifest_path.read_bytes()
    if hashlib.sha256(data).hexdigest()!=digest:return False
    manifest=json.loads(data)
    expected=set(manifest['sheets'])|{'manifest.json'}
    if {entry.name for entry in path.iterdir()}!=expected:return False
    for name,checksum in manifest['sheets'].items():
        file=path/name
        if Path(name).name!=name or file.is_symlink() or not file.is_file():return False
        with file.open('rb') as stream:
            if hashlib.file_digest(stream,'sha256').hexdigest()!=checksum:return False
    return True


def prune_generations(database,artifacts,projection,checkpoint=lambda:None):
    """Caller holds preview-cache.lock; retain current + one superseded generation.

    Projection references pin an old generation until synchronization completes.
    Registration precedes publication, covering crashes between rename and commit.
    Unregistered/incomplete scratch directories are never inferred to be ours.
    """
    root=Path(artifacts).resolve()/'packed-previews'
    removed=0
    with connection(database) as db:
        ensure_generation_schema(db)
        candidates=db.execute('''SELECT g.* FROM preview_generations g WHERE g.obsolete=1 AND g.review IS NULL
            AND NOT EXISTS (SELECT 1 FROM preview_packs p WHERE p.directory=g.directory)
            AND (SELECT count(*) FROM preview_generations newer WHERE newer.asset=g.asset AND newer.id>g.id
                 AND NOT EXISTS (SELECT 1 FROM preview_packs p WHERE p.directory=newer.directory))>=?
            ORDER BY g.id LIMIT ?''',(RETAINED_SUPERSEDED_PACKS,CLEANUP_BATCH_SIZE)).fetchall()
    for candidate in candidates:
        checkpoint()
        directory=Path(candidate['directory']);staging=Path(candidate['staging'])
        with connection(database) as db:
            # Keep the catalog writer lock through verification/removal so a
            # concurrent publication cannot make this generation current again.
            if db.execute('SELECT 1 FROM preview_packs WHERE directory=?',(str(directory),)).fetchone():continue
            source=db.execute('SELECT payload FROM media WHERE asset=?',(candidate['asset'],)).fetchall()
            payloads=[json.loads(row['payload']) for row in source]
            with contextlib.closing(sqlite3.connect(Path(projection).resolve().as_uri()+'?mode=ro',uri=True)) as visible:
                payloads += [json.loads(row[0]) for row in visible.execute('SELECT summary FROM files WHERE asset=?',(candidate['asset'],))]
                referenced=[row[0] for row in visible.execute('SELECT frame FROM frames WHERE asset=?',(candidate['asset'],))]
            for payload in payloads:
                referenced += [frame['frame'] for frame in payload.get('frames',[])]
                if payload.get('match'):referenced.append(payload['match']['frame'])
            referenced += [json.loads(row[0])['frame'] for row in db.execute("SELECT payload FROM index_units WHERE asset=? AND stage='frame'",(candidate['asset'],))]
            if any(Path(frame).parent==directory for frame in referenced):continue
            paths=[path for path in (directory,staging) if path.exists() or path.is_symlink()]
            if not all(verified_directory(path,root,candidate['digest']) for path in paths):
                db.execute('UPDATE preview_generations SET review=? WHERE id=?',('Changed or unfamiliar files retained',candidate['id']))
                continue
            for path in paths:shutil.rmtree(path)
            if paths:
                descriptor=os.open(root,os.O_RDONLY)
                try:os.fsync(descriptor)
                finally:os.close(descriptor)
            db.execute('DELETE FROM preview_generations WHERE id=?',(candidate['id'],))
            removed+=len(paths)
    return removed
