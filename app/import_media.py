"""Resumable card/folder imports with optional verified source removal.

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
import plistlib
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import uuid

from index_queue import EXTENSIONS, Queue, Stopped
from index_store import register_verified_import

CHUNK = 4 * 1024 * 1024


def sync_original(descriptor):
    os.fsync(descriptor)
    if sys.platform == 'darwin':
        # macOS fsync alone need not flush a drive's write cache. Source removal
        # requires the stronger full-sync operation to succeed too.
        fcntl.fcntl(descriptor, 51)  # F_FULLFSYNC


class Importer:
    def __init__(self, source, destination, device, catalog=None, emit=lambda value: None, date_reader=None,
                 remove_source=False, policy_json='{}', direct_destination=False, include_proxies=False, eject_after=False):
        self.eject_after = eject_after
        self.ejection = None
        self.include_proxies = include_proxies
        self.direct_destination = direct_destination
        self.local_candidates_only = False
        self.source, self.destination = Path(source).resolve(), Path(destination).resolve()
        if self.source == self.destination or self.source in self.destination.parents or self.destination in self.source.parents:
            raise ValueError('Choose a card or folder outside Originals')
        device = device.strip()
        if not re.fullmatch(r'[\w .-]{1,100}', device) or device in ('.', '..') or device.startswith('.'):
            raise ValueError('Use a device folder name with letters, numbers, spaces, dashes or underscores')
        self.device, self.catalog, self.emit = device, catalog, emit
        self.remove_source = remove_source
        self.policies = json.loads(policy_json)
        if not isinstance(self.policies, dict) or any(v not in ('skip', 'keep', 'remove') for v in self.policies.values()):
            raise ValueError('Invalid per-file import policies')
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
            db.execute('CREATE TABLE IF NOT EXISTS removals(id TEXT PRIMARY KEY, source TEXT, signature TEXT, destination TEXT, digest TEXT, state TEXT, verified_at TEXT)')
        self.done = self.total = self.copied = self.duplicates = self.failed = self.skipped = 0
        self.current, self.phase = '', 'Ready to import'
        self.removed = 0
        self.capture_times = {}

    @contextlib.contextmanager
    def db(self):
        db = sqlite3.connect(self.database)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('PRAGMA fullfsync=ON')
            with db:
                yield db
        finally:
            db.close()

    def status(self, error=None, bytes_done=0, bytes_total=0, catalog_changed=False):
        self.emit(dict(phase=self.phase, current=self.current, done=self.done, total=self.total,
                       copied=self.copied, duplicates=self.duplicates, failed=self.failed, skipped=self.skipped,
                        removed=self.removed, paused=self.paused.is_set(), bytes_done=bytes_done, bytes_total=bytes_total, error=error, ejection=self.ejection,
                        catalog_changed=catalog_changed))

    def publish_catalog(self, source, target, row):
        if self.catalog is None or target.suffix.lower() not in EXTENSIONS:
            return
        captured = self.capture_times.get(str(source))
        if captured is None:
            captured = (json.loads(row['signature'])[1] / 1e9 if self.device == 'iCloud'
                        else datetime.strptime(row['date'], '%Y-%m-%d').timestamp())
        if register_verified_import(self.catalog, target, row['digest'], capture_time=captured, source_device=self.device):
            self.status(catalog_changed=True)

    @staticmethod
    def volume_info(path):
        path = Path(path).resolve(strict=True)
        while not os.path.ismount(path):
            path = path.parent
        return plistlib.loads(subprocess.check_output(['/usr/sbin/diskutil', 'info', '-plist', str(path)], timeout=15))

    def eject_volume(self, volume):
        self.checkpoint()
        current = self.volume_info(self.source)
        keys = ('VolumeUUID', 'DeviceIdentifier', 'ParentWholeDisk', 'MountPoint')
        if any(not volume.get(k) or current.get(k) != volume[k] for k in keys):
            raise RuntimeError('Camera volume changed; eject it manually')
        result = subprocess.run(['/usr/sbin/diskutil', 'eject', volume['ParentWholeDisk']],
                                capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise RuntimeError('Copies are saved, but the device could not be ejected. Close other apps using it and eject in Finder. ' + result.stderr.strip())
        self.ejection = 'Safe to unplug — device ejected'

    def policy(self, source):
        return self.policies.get(str(source.relative_to(self.source)), 'remove' if self.remove_source else 'keep')

    def remove_verified(self, source):
        """Never infer permission or successful verification from a prior run."""
        self.checkpoint()
        signature = Queue.signature(source)
        with self.db() as db:
            row = db.execute('SELECT * FROM files WHERE source=? AND signature=? AND device=?',
                             (str(source), signature, self.device)).fetchone()
        if row is None or not row['destination']:
            raise RuntimeError('No verified import record; source retained')
        target = Path(row['destination'])
        if source.is_symlink() or target.is_symlink() or not target.is_file() or self.destination not in target.resolve().parents:
            raise RuntimeError('Removal requires an independent original inside Originals; source retained')
        if self.source not in source.resolve().parents or os.path.samefile(source, target):
            raise RuntimeError('Source is not independent of the destination; source retained')
        # Fresh hashes, including resumed imports and previously imported duplicates.
        # fsync before the destination read and flush its directory entry as well.
        with target.open('rb') as saved:
            sync_original(saved.fileno())
        directory = os.open(target.parent, os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
        target_signature = Queue.signature(target)
        self.phase = 'Rechecking source before removal'
        source_digest = self.digest(source)
        self.phase = 'Rechecking saved original before removal'
        if self.digest(target) != row['digest'] or source_digest != row['digest']:
            raise RuntimeError('Final hash verification failed; source retained')
        receipt = uuid.uuid4().hex
        with self.db() as db:
            db.execute('INSERT INTO removals VALUES(?,?,?,?,?,?,?)',
                       (receipt, str(source), signature, str(target), row['digest'], 'verified', datetime.now().isoformat()))
        self.checkpoint()
        if source.is_symlink() or target.is_symlink() or Queue.signature(source) != signature or Queue.signature(target) != target_signature:
            raise RuntimeError('A file changed after verification; source retained')
        source.unlink()
        directory = os.open(source.parent, os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
        with self.db() as db:
            db.execute("UPDATE removals SET state='removed' WHERE id=?", (receipt,))
        self.removed += 1

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
        captured, origin = Importer.capture_time(path)
        return captured.strftime('%Y-%m-%d'), origin

    @staticmethod
    def capture_time(path):
        result = subprocess.run(['/opt/homebrew/bin/ffprobe', '-v', 'error', '-show_entries',
                                 'format_tags:stream_tags', '-of', 'json', str(path)], capture_output=True, text=True, check=True, timeout=60)
        probe = json.loads(result.stdout)
        tags = [probe.get('format', {}).get('tags', {})] + [s.get('tags', {}) for s in probe.get('streams', [])]
        for tag in tags:
            raw = tag.get('com.apple.quicktime.creationdate') or tag.get('creation_time')
            if raw:
                try:
                    return datetime.fromisoformat(raw.replace('Z', '+00:00')), 'capture metadata'
                except ValueError:
                    pass
        if path.suffix.lower() in ('.jpg', '.jpeg', '.png', '.heic'):
            try:
                from PIL import Image
                with Image.open(path) as image:
                    exif = image.getexif()
                    raw = exif.get_ifd(34665).get(36867) or exif.get(306)
                    if raw:
                        return datetime.strptime(str(raw), '%Y:%m:%d %H:%M:%S'), 'capture metadata'
            except (OSError, ValueError):
                pass
        return datetime.fromtimestamp(path.stat().st_mtime), 'file modification date'

    @staticmethod
    def priority(path):
        # DJI includes the full capture time in both original and proxy names.
        match = re.match(r'DJI_(\d{14})_', path.name, re.IGNORECASE)
        if match:
            try:
                return datetime.strptime(match[1], '%Y%m%d%H%M%S').timestamp()
            except ValueError:
                pass
        try:
            return Importer.capture_time(path)[0].timestamp()
        except (OSError, ValueError, subprocess.SubprocessError):
            return path.stat().st_mtime

    def candidates(self, digest):
        with self.db() as db:
            found = [Path(r[0]) for r in db.execute('SELECT destination FROM files WHERE digest=? AND destination IS NOT NULL', (digest,))]
        if self.catalog and Path(self.catalog).is_file():
            with contextlib.closing(sqlite3.connect(Path(self.catalog).resolve().as_uri() + '?mode=ro', uri=True)) as db:
                found += [Path(r[0]) for r in db.execute('SELECT path FROM media WHERE asset=?', ('sha256:' + digest,))]
        return [path for path in dict.fromkeys(found)
                if not self.local_candidates_only or self.destination in path.resolve().parents]

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
                self.publish_catalog(source, candidate, row)
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
        base = self.destination if self.direct_destination else self.destination / self.device
        if source.suffix.lower() == '.lrf':
            base = self.destination / '.mami-proxies' / self.device
        folder = base / row['date'][:4] / row['date']
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
        self.publish_catalog(source, target, row)
        return 'copied'

    def run(self):
        with (self.directory / 'import.lock').open('a+b') as owner:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.phase = 'Finding media'
            files = []
            if not self.source.is_dir():
                raise FileNotFoundError('Source folder is unavailable; reconnect the device and try again')
            source_device = self.source.stat().st_dev
            volume = None
            if self.eject_after and sys.platform == 'darwin':
                info = self.volume_info(self.source)
                destination_info = self.volume_info(self.destination)
                if info.get('Internal') is False and info.get('ParentWholeDisk') and info.get('VolumeUUID') and info.get('MountPoint') != '/' and info.get('ParentWholeDisk') != destination_info.get('ParentWholeDisk'):
                    volume = info
                else:
                    self.ejection = 'Automatic eject applies to external camera/card volumes only'
            def failed_walk(error):
                raise error
            for root, dirs, names in os.walk(self.source, followlinks=False, onerror=failed_walk):
                self.checkpoint()
                dirs[:] = sorted(d for d in dirs if not d.startswith('.') and not (Path(root) / d).is_symlink())
                for name in sorted(names):
                    path = Path(root) / name
                    if name.startswith('.') or path.is_symlink() or not path.is_file():
                        continue
                    supported = path.suffix.lower() in EXTENSIONS or (self.include_proxies and path.suffix.lower() == '.lrf')
                    if not supported or self.policy(path) == 'skip':
                        self.skipped += 1
                    else:
                        files.append(path)
                self.status()
            self.total = len(files)
            self.capture_times = {str(path): self.priority(path) for path in files}
            files.sort(key=lambda path: self.capture_times[str(path)], reverse=True)
            for source in files:
                self.checkpoint()
                self.current = source.name
                try:
                    if self.source.stat().st_dev != source_device:
                        raise RuntimeError('Camera disconnected or changed; reconnect and retry to resume')
                    result = self.copy_one(source)
                    if result == 'copied': self.copied += 1
                    else: self.duplicates += 1
                    if self.policy(source) == 'remove': self.remove_verified(source)
                except Stopped:
                    raise
                except Exception as error:
                    self.failed += 1
                    self.status(error=str(error))
                self.done += 1
                self.status()
            self.phase, self.current = ('Import needs attention' if self.failed else 'Import complete'), ''
            if not self.source.is_dir() or self.source.stat().st_dev != source_device:
                self.failed += 1
                self.phase = 'Import needs attention'
                self.status(error='Camera disconnected; reconnect and retry to resume saved work')
            if volume and not self.failed and self.total > 0:
                self.phase = 'Ejecting device'
                self.status()
                try:
                    self.eject_volume(volume)
                except Stopped:
                    raise
                except Exception as error:
                    self.ejection = 'Device not ejected'
                    self.phase = 'Import complete'
                    self.status(error=str(error))
                self.phase = 'Import complete'
            self.status()


def main():
    parser = argparse.ArgumentParser()
    for name in ('source', 'destination', 'device'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--catalog')
    parser.add_argument('--direct-destination', action='store_true')
    parser.add_argument('--remove-source', action='store_true')
    parser.add_argument('--eject-after', action='store_true')
    parser.add_argument('--include-proxies', action='store_true', help='Preserve DJI .LRF files in .mami-proxies; source removal still requires a verified independent copy')
    parser.add_argument('--policy-json', default='{}')
    parser.add_argument('--policy-file')
    args = parser.parse_args()
    if args.policy_file:
        args.policy_json = Path(args.policy_file).read_text()
    del args.policy_file
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
