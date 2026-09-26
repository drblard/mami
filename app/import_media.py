"""Resumable card/folder imports. Source files are never changed.

Only verified files are published in Originals. Hidden staging files and the
durable journal are retained, including failed or interrupted attempts.
"""
import argparse
import contextlib
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import threading
import uuid

from index_queue import EXTENSIONS, Queue, Stopped

CHUNK = 4 * 1024 * 1024


class Importer:
    def __init__(self, source, destination, device, catalog=None, emit=lambda value: None, date_reader=None):
        self.source, self.destination = Path(source).resolve(), Path(destination).resolve()
        if self.source == self.destination or self.source in self.destination.parents or self.destination in self.source.parents:
            raise ValueError('Choose a card or folder outside Originals')
        device = device.strip()
        if not re.fullmatch(r'[\w .-]{1,100}', device) or device in ('.', '..') or device.startswith('.'):
            raise ValueError('Use a device folder name with letters, numbers, spaces, dashes or underscores')
        self.device, self.catalog, self.emit = device, catalog, emit
        if not self.device:
            raise ValueError('A device folder name is required')
        self.date_reader = date_reader or self.capture_date
        self.stop, self.paused, self.wake = threading.Event(), threading.Event(), threading.Event()
        self.busy = threading.Event()
        self.directory = self.destination / '.mami-imports'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.database = self.directory / 'journal.sqlite'
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS files(source TEXT, signature TEXT, device TEXT, digest TEXT, date TEXT, date_source TEXT, part TEXT, destination TEXT, PRIMARY KEY(source,signature,device))')
        self.done = self.total = self.copied = self.duplicates = self.failed = self.skipped = 0
        self.current, self.phase = '', 'Ready to import'

    @contextlib.contextmanager
    def db(self):
        db = sqlite3.connect(self.database)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA synchronous=FULL')
            with db:
                yield db
        finally:
            db.close()

    def status(self, error=None, bytes_done=0, bytes_total=0):
        self.emit(dict(phase=self.phase, current=self.current, done=self.done, total=self.total,
                       copied=self.copied, duplicates=self.duplicates, failed=self.failed, skipped=self.skipped,
                       paused=self.paused.is_set(), bytes_done=bytes_done, bytes_total=bytes_total, error=error))

    def checkpoint(self):
        phase = self.phase
        while (self.paused.is_set() or self.busy.is_set()) and not self.stop.is_set():
            if self.busy.is_set(): self.phase = 'Waiting for preview/search'
            self.status()
            self.wake.wait(.2)
            self.wake.clear()
        if self.stop.is_set():
            raise Stopped()
        self.phase = phase

    def digest(self, path):
        digest = hashlib.sha256()
        with path.open('rb') as source:
            while True:
                self.checkpoint()
                chunk = source.read(CHUNK)
                if not chunk:
                    return digest.hexdigest()
                digest.update(chunk)
                self.status(bytes_done=source.tell(), bytes_total=path.stat().st_size)

    @staticmethod
    def capture_date(path):
        result = subprocess.run(['/opt/homebrew/bin/ffprobe', '-v', 'error', '-show_entries',
                                 'format_tags:stream_tags', '-of', 'json', str(path)], capture_output=True, text=True, check=True, timeout=60)
        probe = json.loads(result.stdout)
        tags = [probe.get('format', {}).get('tags', {})] + [s.get('tags', {}) for s in probe.get('streams', [])]
        for tag in tags:
            raw = tag.get('com.apple.quicktime.creationdate') or tag.get('creation_time')
            if raw:
                try:
                    return datetime.fromisoformat(raw.replace('Z', '+00:00')).strftime('%Y-%m-%d'), 'capture metadata'
                except ValueError:
                    pass
        if path.suffix.lower() in ('.jpg', '.jpeg', '.png', '.heic'):
            try:
                from PIL import Image
                with Image.open(path) as image:
                    exif = image.getexif()
                    raw = exif.get_ifd(34665).get(36867) or exif.get(306)
                    if raw:
                        return datetime.strptime(str(raw), '%Y:%m:%d %H:%M:%S').strftime('%Y-%m-%d'), 'capture metadata'
            except (OSError, ValueError):
                pass
        return datetime.fromtimestamp(path.stat().st_mtime).strftime('%Y-%m-%d'), 'file modification date'

    def candidates(self, digest):
        with self.db() as db:
            found = [Path(r[0]) for r in db.execute('SELECT destination FROM files WHERE digest=? AND destination IS NOT NULL', (digest,))]
        if self.catalog and Path(self.catalog).is_file():
            with contextlib.closing(sqlite3.connect(Path(self.catalog).resolve().as_uri() + '?mode=ro', uri=True)) as db:
                found += [Path(r[0]) for r in db.execute('SELECT path FROM media WHERE asset=?', ('sha256:' + digest,))]
        return list(dict.fromkeys(found))

    def copy_one(self, source):
        signature = Queue.signature(source)
        key = (str(source), signature, self.device)
        with self.db() as db:
            row = db.execute('SELECT * FROM files WHERE source=? AND signature=? AND device=?', key).fetchone()
        self.phase = 'Checking source'
        if row is None:
            digest = self.digest(source)
            date, date_source = self.date_reader(source)
            datetime.strptime(date, '%Y-%m-%d')
            if Queue.signature(source) != signature:
                raise RuntimeError('Source changed while reading; retry when copying to the card has finished')
            part = self.directory / (str(uuid.uuid4()) + '.partial')
            with self.db() as db:
                db.execute('INSERT INTO files VALUES(?,?,?,?,?,?,?,NULL)', (*key, digest, date, date_source, str(part)))
                row = db.execute('SELECT * FROM files WHERE source=? AND signature=? AND device=?', key).fetchone()
        digest = row['digest']
        for candidate in self.candidates(digest):
            self.phase = 'Verifying existing copy'
            if candidate.is_file() and self.digest(candidate) == digest:
                if Queue.signature(source) != signature:
                    raise RuntimeError('Source changed during verification')
                with self.db() as db:
                    db.execute('UPDATE files SET destination=? WHERE source=? AND signature=? AND device=?', (str(candidate), *key))
                return 'duplicate'
        part = Path(row['part'])
        self.phase = 'Checking saved copy'
        # Validate saved bytes before resuming. Retain any damaged attempt and
        # begin a fresh one; never truncate or overwrite an existing output.
        offset, reusable = 0, True
        if part.exists():
            with source.open('rb') as original, part.open('rb') as saved:
                while True:
                    self.checkpoint()
                    chunk = saved.read(CHUNK)
                    if not chunk:
                        break
                    if original.read(len(chunk)) != chunk:
                        reusable = False
                        break
                    offset += len(chunk)
                    self.status(bytes_done=offset, bytes_total=source.stat().st_size)
        if not reusable:
            part, offset = self.directory / (str(uuid.uuid4()) + '.partial'), 0
            with self.db() as db:
                db.execute('UPDATE files SET part=? WHERE source=? AND signature=? AND device=?', (str(part), *key))
        self.phase = 'Copying'
        with source.open('rb') as original, part.open('ab' if part.exists() else 'xb') as output:
            original.seek(offset)
            while True:
                self.checkpoint()
                chunk = original.read(CHUNK)
                if not chunk:
                    break
                output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
                offset += len(chunk)
                self.status(bytes_done=offset, bytes_total=source.stat().st_size)
        self.phase = 'Verifying copied bytes'
        if self.digest(part) != digest or Queue.signature(source) != signature:
            raise RuntimeError('Verification failed; saved attempt retained, no original published')
        folder = self.destination / self.device / row['date'][:4] / row['date']
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / source.name
        while True:
            self.checkpoint()
            try:
                # Atomic, no-clobber publication on the destination filesystem.
                # The hidden staging link is retained as a recovery artifact.
                os.link(part, target)
                descriptor = os.open(folder, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                break
            except FileExistsError:
                if target.is_file() and self.digest(target) == digest:
                    break
                target = folder / f'{source.stem}-{digest[:12]}-{uuid.uuid4().hex[:8]}{source.suffix}'
        with self.db() as db:
            db.execute('UPDATE files SET destination=? WHERE source=? AND signature=? AND device=?', (str(target), *key))
        return 'copied'

    def run(self):
        with (self.directory / 'import.lock').open('a+b') as owner:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.phase = 'Finding media'
            files = []
            if not self.source.is_dir():
                raise FileNotFoundError('Source folder is unavailable; reconnect the device and try again')
            def failed_walk(error):
                raise error
            for root, dirs, names in os.walk(self.source, followlinks=False, onerror=failed_walk):
                self.checkpoint()
                dirs[:] = sorted(d for d in dirs if not d.startswith('.') and not (Path(root) / d).is_symlink())
                for name in sorted(names):
                    path = Path(root) / name
                    if name.startswith('.') or path.is_symlink() or not path.is_file():
                        continue
                    if path.suffix.lower() not in EXTENSIONS:
                        self.skipped += 1
                    else:
                        files.append(path)
                self.status()
            self.total = len(files)
            for source in files:
                self.checkpoint()
                self.current = source.name
                try:
                    result = self.copy_one(source)
                    if result == 'copied': self.copied += 1
                    else: self.duplicates += 1
                except Stopped:
                    raise
                except Exception as error:
                    self.failed += 1
                    self.status(error=str(error))
                self.done += 1
                self.status()
            self.phase, self.current = ('Import needs attention' if self.failed else 'Import complete'), ''
            self.status()


def main():
    parser = argparse.ArgumentParser()
    for name in ('source', 'destination', 'device'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--catalog')
    args = parser.parse_args()
    os.nice(10)
    importer = Importer(**vars(args), emit=lambda value: print(json.dumps(value), flush=True))
    signal.signal(signal.SIGTERM, lambda *_: (importer.stop.set(), importer.wake.set()))
    def commands():
        import sys
        try:
            for line in sys.stdin:
                action = json.loads(line).get('action')
                if action == 'pause': importer.paused.set()
                elif action == 'resume': importer.paused.clear()
                elif action == 'stop': importer.stop.set()
                elif action == 'busy': importer.busy.set()
                elif action == 'idle': importer.busy.clear()
                importer.wake.set()
        finally:
            importer.stop.set(); importer.wake.set()
    threading.Thread(target=commands, daemon=True).start()
    try:
        importer.run()
    except Stopped:
        importer.phase = 'Import stopped — progress saved'
        importer.status()
    except Exception as error:
        importer.phase = 'Import needs attention'
        importer.status(error=str(error))


if __name__ == '__main__':
    main()
