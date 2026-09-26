"""Consume only published Photos receipts; never access the Photos library itself."""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys
import threading

from import_media import Importer, Queue, Stopped, sync_original


class PhotosBatch:
    def __init__(self, importer):
        self.importer = importer
        importer.local_candidates_only = True

    def complete(self, receipt_path):
        imp = self.importer
        receipt_path = Path(receipt_path)
        if receipt_path.parent.resolve() != imp.source / '.receipts' or receipt_path.is_symlink():
            raise ValueError('Receipt is outside Photos staging')
        receipt_path = receipt_path.resolve()
        receipt = json.loads(receipt_path.read_text())
        source = imp.source / receipt['file']
        if source.is_symlink() or imp.source not in source.resolve().parents:
            raise ValueError('Original is outside Photos staging')
        key = receipt_path.stem
        with self.catalog() as db:
            completed = db.execute('SELECT 1 FROM photos_import_history WHERE resource=?', (key,)).fetchone()
        # An archived/offline completed original is not a request for another copy.
        if completed and not source.exists():
            return
        imp.current = source.name
        if source.stat().st_size != receipt['size'] or imp.digest(source) != receipt['digest']:
            raise ValueError('Photos staging failed receipt verification')
        # These are redundant names for the same staged inode, not independent
        # originals. Release them before journaling its ctime-sensitive signature.
        # The published source name remains intact until final verification.
        for alias in receipt_path.parent.glob('*.partial'):
            try:
                if not alias.is_symlink() and os.path.samefile(alias, source):
                    alias.unlink()
            except FileNotFoundError:
                pass
        result = 'duplicate' if completed else imp.copy_one(source)
        signature = Queue.signature(source)
        with imp.db() as db:
            row = db.execute('SELECT * FROM files WHERE source=? AND signature=? AND device=?',
                             (str(source), signature, imp.device)).fetchone()
        if completed and (row is None or not row['destination'] or not Path(row['destination']).is_file()):
            return
        target = Path(row['destination'])
        # Only an independent copy in this destination can authorize staging cleanup.
        if target.is_symlink() or imp.destination not in target.resolve().parents or os.path.samefile(source, target):
            raise ValueError('Verified independent destination required')
        with target.open('rb') as saved:
            sync_original(saved.fileno())
        if imp.digest(target) != receipt['digest'] or Queue.signature(source) != signature:
            raise ValueError('Destination changed before completion')
        with self.catalog() as db:
            db.execute('INSERT OR IGNORE INTO photos_import_history VALUES(?,?,?)',
                       (key, receipt['digest'], str(receipt['size'])))
        receipt['imported'] = True
        receipt['destination'] = str(target)
        temporary = receipt_path.with_suffix('.updating')
        with temporary.open('w') as saved:
            json.dump(receipt, saved)
            saved.flush()
            sync_original(saved.fileno())
        os.replace(temporary, receipt_path)
        fd = os.open(receipt_path.parent, os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
        imp.remove_verified(source)
        part = Path(row['part'])
        if part.is_file() and not part.is_symlink() and os.path.samefile(part, target):
            part.unlink()
        if result == 'copied': imp.copied += 1
        else: imp.duplicates += 1

    @contextlib.contextmanager
    def catalog(self):
        path = Path(self.importer.catalog)
        with (path.parent / 'catalog.lock').open('a+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with contextlib.closing(sqlite3.connect(path)) as db:
                db.execute('PRAGMA journal_mode=PERSIST')
                db.execute('PRAGMA synchronous=FULL')
                db.execute('PRAGMA fullfsync=ON')
                with db:
                    yield db


def main():
    parser = argparse.ArgumentParser()
    for name in ('source', 'destination', 'catalog', 'manifest'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    receipts = json.loads(Path(args.manifest).read_text())
    os.nice(10)
    imp = Importer(args.source, args.destination, 'iCloud', catalog=args.catalog,
                   direct_destination=True, emit=lambda value: print(json.dumps(value), flush=True))
    def stop(*_):
        imp.stop.set(); imp.wake.set()
    signal.signal(signal.SIGTERM, stop)
    def commands():
        try:
            for line in sys.stdin:
                action = json.loads(line).get('action')
                if action == 'stop': stop()
                elif action == 'pause': imp.paused.set()
                elif action == 'resume': imp.paused.clear()
                elif action == 'busy': imp.busy.set()
                elif action == 'idle': imp.busy.clear()
                imp.wake.set()
        finally: stop()
    threading.Thread(target=commands, daemon=True).start()
    try:
        with (imp.directory / 'import.lock').open('a+b') as owner:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            imp.total = len(receipts)
            batch = PhotosBatch(imp)
            for receipt in receipts:
                imp.checkpoint()
                batch.complete(receipt)
                imp.done += 1
                imp.phase = 'Original saved and verified'
                imp.status()
            imp.phase = 'Import complete'
            imp.status()
    except Exception as error:
        imp.failed += 1
        imp.phase = 'Photos transfer stopped' if isinstance(error, Stopped) else 'Photos transfer needs attention'
        imp.status(error=str(error))
        sys.exit(1)


if __name__ == '__main__':
    main()
