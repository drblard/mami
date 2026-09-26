"""Verify and restore a standalone catalog snapshot into a NEW directory.

Never overwrites the live database, a previous restore, or the input snapshot.
"""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3


def inspect_snapshot(path):
    with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('Snapshot failed SQLite integrity check')
        version = db.execute('PRAGMA user_version').fetchone()[0]
        if version != 2:
            raise ValueError(f'Unsupported catalog schema: {version}')
        identity, revision, change_token = db.execute('SELECT identity,revision,change_token FROM state WHERE id=1').fetchone()
        counts = {table: db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                  for table in ['media', 'annotations', 'annotation_history']}
        return dict(identity=identity, revision=revision, change_token=change_token, **counts)


def restore(source, destination):
    source, destination = Path(source), Path(destination)
    summary = inspect_snapshot(source)
    destination.mkdir(parents=True, exist_ok=False)
    target = destination / 'catalog.sqlite'
    with source.open('rb') as original, target.open('xb') as output:
        shutil.copyfileobj(original, output)
        output.flush()
        os.fsync(output.fileno())
    if inspect_snapshot(target) != summary:
        raise ValueError('Restored catalog differs from its source')
    with source.open('rb') as original, target.open('rb') as output:
        digest = hashlib.file_digest(original, 'sha256').hexdigest()
        if digest != hashlib.file_digest(output, 'sha256').hexdigest():
            raise ValueError('Restored catalog checksum differs from its source')
    receipt = dict(source=str(source.resolve()), sha256=digest, **summary)
    with (destination / 'restore.json').open('x') as output:
        json.dump(receipt, output, indent=2)
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot')
    parser.add_argument('--to', required=True, help='New directory, not the current live catalog')
    args = parser.parse_args()
    print(json.dumps(restore(args.snapshot, args.to), indent=2))
    print(f'Launch Mami with MAMI_CATALOG={Path(args.to).resolve()} to use this restored copy.')
