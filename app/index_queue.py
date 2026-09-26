"""Durable, cooperative background scan/index queue. Originals are read-only.

Each successful file hash, frame, embedding and 30-second speech chunk commits
independently. Artifacts are fsynced before SQLite references them. Interrupted
artifacts are retained; only the unfinished unit is retried after a crash.
"""
import contextlib
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid

PIPELINE = 'siglip2-75de2d55-whisper-a4aaeec0-1s-ro-chunk30-v1'
EXTENSIONS = {'.mp4': 'video', '.mov': 'video', '.jpg': 'image', '.jpeg': 'image', '.png': 'image', '.heic': 'image'}


class Stopped(Exception):
    pass

class Reprioritize(Exception):
    pass


class Queue:
    def __init__(self, database, root, artifacts, backend, emit=lambda event: None, gpu_wait=None):
        self.database, self.root, self.artifacts = map(Path, (database, root, artifacts))
        self.backend, self.emit = backend, emit
        self.gpu_wait = gpu_wait
        self.gpu_utilization = None
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.busy = threading.Event()
        self.gpu_busy = threading.Event()
        self.paused = threading.Event()
        self.phase, self.done, self.total, self.current = 'Waiting', 0, 0, ''
        self.last_emit = 0
        self.waiting = False
        self.scan_errors = 0
        self.scan_requested = threading.Event()
        self.last_scan = time.monotonic()
        self.active_job = False
        self.initialize()

    @contextlib.contextmanager
    def db(self):
        with (self.database.parent / 'catalog.lock').open('a+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            db = sqlite3.connect(self.database, timeout=5)
            try:
                db.row_factory = sqlite3.Row
                db.execute('PRAGMA journal_mode=PERSIST')
                db.execute('PRAGMA synchronous=FULL')
                db.execute('BEGIN IMMEDIATE')
                with db:
                    yield db
            finally:
                db.close()
                fcntl.flock(lock, fcntl.LOCK_UN)

    def initialize(self):
        with self.db() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='index_schema'").fetchone():
                db.execute('CREATE TABLE index_schema(version INTEGER NOT NULL)')
                db.execute('INSERT INTO index_schema VALUES(1)')
                db.execute('CREATE TABLE scan_control(id INTEGER PRIMARY KEY, paused INTEGER NOT NULL)')
                db.execute('INSERT INTO scan_control VALUES(1,0)')
                db.execute('CREATE TABLE scan_files(path TEXT PRIMARY KEY, signature TEXT NOT NULL, asset TEXT NOT NULL)')
                db.execute('CREATE TABLE index_jobs(asset TEXT PRIMARY KEY, path TEXT NOT NULL, kind TEXT NOT NULL, signature TEXT NOT NULL, logical TEXT NOT NULL, state TEXT NOT NULL, error TEXT, attempts INTEGER NOT NULL DEFAULT 0)')
                db.execute('CREATE TABLE index_units(asset TEXT NOT NULL, pipeline TEXT NOT NULL, stage TEXT NOT NULL, ordinal INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(asset,pipeline,stage,ordinal))')
                for table in ('scan_control', 'scan_files', 'index_jobs', 'index_units'):
                    for op in ('INSERT', 'UPDATE', 'DELETE'):
                        db.execute(f'CREATE TRIGGER {table}_{op} AFTER {op} ON {table} BEGIN UPDATE state SET revision=revision+1,change_token=lower(hex(randomblob(16))) WHERE id=1; END')
                db.execute('UPDATE state SET revision=revision+1,change_token=lower(hex(randomblob(16))) WHERE id=1')
            if db.execute('SELECT version FROM index_schema').fetchone()[0] != 1:
                raise ValueError('Unsupported indexing queue version')
            if 'capture_time' not in {r['name'] for r in db.execute('PRAGMA table_info(index_jobs)')}:
                db.execute('ALTER TABLE index_jobs ADD COLUMN capture_time REAL')
            if db.execute('SELECT paused FROM scan_control WHERE id=1').fetchone()[0]:
                self.paused.set()
            # Only jobs interrupted mid-flight change on startup.
            db.execute("UPDATE index_jobs SET state='queued' WHERE state='running'")

    def status(self, force=False, changed=False, error=None):
        now = time.monotonic()
        if force or changed or error or now - self.last_emit >= .2:
            self.last_emit = now
            self.emit(dict(phase=self.phase, done=self.done, total=self.total, current=self.current,
                           paused=self.paused.is_set(), busy=self.busy.is_set(), waiting=self.waiting, gpu_utilization=self.gpu_utilization, changed=changed, error=error))

    def command(self, command):
        action = command.get('action')
        if action in ('pause', 'resume'):
            paused = action == 'pause'
            # Set the in-memory flag immediately; persist before acknowledging.
            self.paused.set() if paused else self.paused.clear()
            with self.db() as db:
                db.execute('UPDATE scan_control SET paused=? WHERE id=1 AND paused != ?', (int(paused), int(paused)))
        elif action == 'busy':
            self.busy.set() if command.get('value') else self.busy.clear()
        elif action == 'gpu-busy':
            self.gpu_busy.set() if command.get('value') else self.gpu_busy.clear()
        elif action == 'retry':
            with self.db() as db:
                db.execute("UPDATE index_jobs SET state='queued',attempts=0,error=NULL WHERE state='error'")
        elif action == 'stop':
            self.stop.set()
        if action in ('scan', 'retry'):
            self.scan_requested.set()
        self.wake.set()
        self.status(force=True)

    def checkpoint(self, gpu=False):
        phase = self.phase
        while self.paused.is_set() or self.busy.is_set() or (gpu and self.gpu_busy.is_set()):
            self.waiting = True
            if self.stop.is_set():
                raise Stopped()
            if gpu and self.gpu_busy.is_set():
                self.phase = 'Waiting for GPU'
            self.status()
            self.wake.wait(.2)
            self.wake.clear()
        if self.stop.is_set():
            raise Stopped()
        if self.active_job and ((self.scan_requested.is_set() and time.monotonic() - self.last_scan >= 15) or time.monotonic() - self.last_scan >= 60):
            raise Reprioritize()
        if gpu and self.gpu_wait:
            self.gpu_wait(self)
            self.checkpoint()
        self.waiting = False
        self.phase = phase

    @staticmethod
    def signature(path):
        s = path.stat()
        return json.dumps([s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_ino, s.st_dev])

    def fingerprint(self, path):
        signature = self.signature(path)
        digest = hashlib.sha256()
        with path.open('rb') as source:
            while True:
                self.checkpoint()
                chunk = source.read(8 * 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                # Yield CPU/I/O time to interactive playback and search.
                time.sleep(.001)
        if self.signature(path) != signature:
            raise RuntimeError('File changed while hashing; retry on the next scan')
        return 'sha256:' + digest.hexdigest(), signature

    def scan(self):
        self.scan_requested.clear()
        self.scan_errors = 0
        self.phase, self.done, self.total, self.current = 'Discovering files', 0, 0, ''
        self.status(force=True)
        roots = {self.root.resolve()}
        with self.db() as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='media_roots'").fetchone():
                roots.update(Path(row[0]).resolve() for row in db.execute('SELECT path FROM media_roots'))
        available = {root for root in roots if root.is_dir()}
        # Nested destinations are already covered by their parent. Offline roots
        # retain their catalog entries and are retried on the next scan.
        directories = [root for root in available if not any(parent in available for parent in root.parents)]
        if not directories:
            raise FileNotFoundError('Media folders unavailable; reconnect the destination drive')
        files = []
        imported_times = {}
        while directories:
            self.checkpoint()
            directory = directories.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    self.checkpoint()
                    if entry.name == '.mami-imports' and not entry.is_symlink():
                        journal = Path(entry.path) / 'journal.sqlite'
                        if journal.is_file():
                            # Photos exports preserve PhotoKit creation time as
                            # source mtime. The durable import journal retains it
                            # even after staging removal. Prefer that authoritative
                            # date to edited media tags or destination copy time.
                            try:
                                with contextlib.closing(sqlite3.connect(journal.as_uri() + '?mode=ro', uri=True)) as imports:
                                    for digest, signature in imports.execute("SELECT digest,signature FROM files WHERE device='iCloud' AND destination IS NOT NULL"):
                                        imported_times['sha256:' + digest] = json.loads(signature)[1] / 1e9
                            except (sqlite3.Error, ValueError, TypeError, IndexError):
                                pass
                    if entry.name.startswith('.') or entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        directories.append(Path(entry.path))
                    elif Path(entry.name).suffix.lower() in EXTENSIONS and entry.is_file(follow_symlinks=False):
                        files.append(Path(entry.path))
                    self.done = len(files)
                    self.status()
        self.phase, self.done, self.total = 'Checking media', 0, len(files)
        self.status(force=True)
        for path in sorted(files):
            self.checkpoint()
            self.current = path.name
            try:
                signature = self.signature(path)
                with self.db() as db:
                    saved = db.execute('SELECT signature,asset FROM scan_files WHERE path=?', (str(path),)).fetchone()
                if saved and saved['signature'] == signature:
                    asset = saved['asset']
                else:
                    asset, signature = self.fingerprint(path)
                changed = False
                with self.db() as db:
                    db.execute('INSERT INTO scan_files VALUES(?,?,?) ON CONFLICT(path) DO UPDATE SET signature=excluded.signature,asset=excluded.asset WHERE scan_files.signature != excluded.signature OR scan_files.asset != excluded.asset', (str(path), signature, asset))
                    known = db.execute('SELECT path,payload FROM media WHERE asset=?', (asset,)).fetchone()
                    job = db.execute('SELECT state,capture_time FROM index_jobs WHERE asset=?', (asset,)).fetchone()
                    if known:
                        if known['path'] != str(path) and not Path(known['path']).exists():
                            media = json.loads(known['payload'])
                            media['url'] = path.as_uri()
                            db.execute('UPDATE media SET path=?,payload=? WHERE path=?', (str(path), json.dumps(media, sort_keys=True), known['path']))
                            changed = True
                        # Seeded experimental assets already have their index.
                        # New assets with partial previews still have a queue job.
                    if not known and not job:
                        db.execute("INSERT INTO index_jobs(asset,path,kind,signature,logical,state) VALUES(?,?,?,?,?,'queued')", (asset, str(path), EXTENSIONS[path.suffix.lower()], signature, 'library:' + asset))
                    elif job:
                        db.execute('UPDATE index_jobs SET path=?,signature=? WHERE asset=? AND (path != ? OR signature != ?)', (str(path), signature, asset, str(path), signature))
                imported_time = imported_times.get(asset)
                if (not known and not job) or (job and job['state'] != 'complete' and (job['capture_time'] is None or (imported_time is not None and job['capture_time'] != imported_time))):
                    queued = dict(asset=asset, path=str(path), kind=EXTENSIONS[path.suffix.lower()], signature=signature)
                    capture_time = imported_time
                    if capture_time is None:
                        probe = self.read_probe(queued)
                        raw = (probe.get('metadata') or {}).get('sortDate')
                        try:
                            capture_time = datetime.strptime(str(raw), '%Y%m%d%H%M%S').timestamp()
                        except (ValueError, TypeError, OverflowError):
                            capture_time = path.stat().st_mtime
                    with self.db() as db:
                        db.execute('UPDATE index_jobs SET capture_time=? WHERE asset=?', (capture_time, asset))
                if changed:
                    self.status(changed=True)
            except Stopped:
                raise
            except Exception as error:
                self.scan_errors += 1
                self.status(error=f'{path.name}: {error}')
            self.done += 1
            self.status()
        self.repair_missing_artifacts()
        version = getattr(self.backend, 'metadata_version', 0)
        if version:
            with self.db() as db:
                for row in db.execute("SELECT asset,payload FROM index_units WHERE pipeline=? AND stage='metadata' AND ordinal=0", (PIPELINE,)).fetchall():
                    if json.loads(row['payload']).get('metadataVersion', 0) < version:
                        db.execute("UPDATE index_jobs SET state='queued',attempts=0,error=NULL WHERE asset=? AND state IN ('complete','error')", (row['asset'],))

    def repair_missing_artifacts(self):
        # Completed jobs still need a lightweight artifact audit. Do no inference
        # and hold no catalog lock while checking the filesystem.
        with self.db() as db:
            rows = db.execute("SELECT u.asset,u.stage,u.payload FROM index_units u JOIN index_jobs j ON j.asset=u.asset WHERE j.state='complete' AND u.pipeline=? AND u.stage IN ('frame','embedding')", (PIPELINE,)).fetchall()
        damaged = set()
        for row in rows:
            self.checkpoint()
            value = json.loads(row['payload'])
            key = 'frame' if row['stage'] == 'frame' else 'vector'
            if not Path(value[key]).is_file():
                damaged.add(row['asset'])
        if damaged:
            with self.db() as db:
                db.executemany("UPDATE index_jobs SET state='queued',attempts=0,error=NULL WHERE asset=? AND state='complete'", [(asset,) for asset in damaged])

    def unit(self, asset, stage, ordinal):
        with self.db() as db:
            row = db.execute('SELECT payload FROM index_units WHERE asset=? AND pipeline=? AND stage=? AND ordinal=?', (asset, PIPELINE, stage, ordinal)).fetchone()
        if row:
            value = json.loads(row[0])
            # Missing artifacts are repaired, while valid checkpoints are reused.
            if all(Path(value[k]).is_file() for k in ('frame', 'vector', 'audio') if k in value):
                return value
        return None

    def store_unit(self, asset, stage, ordinal, payload):
        for key in ('frame', 'vector', 'audio'):
            if key in payload:
                with open(payload[key], 'rb') as file:
                    os.fsync(file.fileno())
        with self.db() as db:
            db.execute('INSERT INTO index_units VALUES(?,?,?,?,?) ON CONFLICT(asset,pipeline,stage,ordinal) DO UPDATE SET payload=excluded.payload WHERE index_units.payload != excluded.payload', (asset, PIPELINE, stage, ordinal, json.dumps(payload, sort_keys=True)))

    def target(self, asset, suffix):
        folder = self.artifacts / asset.split(':')[-1]
        folder.mkdir(parents=True, exist_ok=True)
        return folder / (str(uuid.uuid4()) + suffix)

    def valid_source(self, job):
        if self.signature(Path(job['path'])) != job['signature']:
            raise RuntimeError('Original changed during indexing; rescan required')

    def publish(self, job, metadata, frames):
        self.valid_source(job)
        value = dict(path=job['logical'], kind=job['kind'], url=Path(job['path']).as_uri(), frames=frames,
                     match=frames[0], metadata=metadata, assetID=job['asset'])
        with self.db() as db:
            existing = db.execute('SELECT asset,payload FROM media WHERE path=?', (job['path'],)).fetchone()
            if existing and existing['asset'] == job['asset'] and len(json.loads(existing['payload'])['frames']) > len(frames):
                return
            before = db.total_changes
            db.execute('INSERT INTO media VALUES(?,?,?) ON CONFLICT(path) DO UPDATE SET asset=excluded.asset,payload=excluded.payload WHERE media.payload != excluded.payload',
                       (job['path'], job['asset'], json.dumps(value, sort_keys=True)))
            changed = db.total_changes != before
        if changed:
            self.status(changed=True)

    def read_probe(self, job):
        asset = job['asset']
        probe = self.unit(asset, 'metadata', 0)
        if probe is None or probe.get('metadataVersion', 0) < getattr(self.backend, 'metadata_version', 0):
            refreshed = self.backend.probe(Path(job['path']), job['kind'])
            if probe is None:
                probe = refreshed
            else:
                # Metadata repair must not invalidate timestamp-aligned vectors
                # or already-completed speech/frame checkpoints.
                probe['metadata'] = refreshed['metadata']
                probe['metadataVersion'] = refreshed.get('metadataVersion', 0)
            with self.db() as db:
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='photos_import_history'").fetchone():
                    if db.execute('SELECT 1 FROM photos_import_history WHERE digest=? LIMIT 1', (asset.removeprefix('sha256:'),)).fetchone():
                        if probe.get('metadata') is not None:
                            probe['metadata']['source'] = 'iCloud'
            self.valid_source(job)
            self.store_unit(asset, 'metadata', 0, probe)
        return probe

    def run_job(self, job):
        asset = job['asset']
        self.valid_source(job)
        with self.db() as db:
            db.execute("UPDATE index_jobs SET state='running',error=NULL WHERE asset=?", (asset,))
        self.phase, self.current, self.done, self.total = 'Reading metadata', Path(job['path']).name, 0, 0
        self.status(force=True)
        self.checkpoint()
        probe = self.read_probe(job)
        timestamps = probe['timestamps']
        speech_times = probe['speech_times']
        self.total = len(timestamps) * 2 + len(speech_times)
        self.done = 0
        frames = []
        for ordinal, timestamp in enumerate(timestamps):
            self.checkpoint()
            self.valid_source(job)
            self.phase = 'Preparing previews'
            sample = self.unit(asset, 'frame', ordinal)
            if sample is None:
                target = self.target(asset, '.jpg')
                self.backend.frame(Path(job['path']), target, timestamp)
                self.valid_source(job)
                sample = dict(path=job['logical'], kind=job['kind'], timestamp=timestamp, frame=str(target))
                self.store_unit(asset, 'frame', ordinal, sample)
            frames.append(sample)
            self.done += 1
            if ordinal == 0:
                self.publish(job, probe['metadata'], frames)
            self.status()
            self.checkpoint()
            self.phase = 'Indexing visual content'
            embedding = self.unit(asset, 'embedding', ordinal)
            if embedding is None:
                target = self.target(asset, '.npy')
                self.backend.embedding(Path(sample['frame']), target)
                self.valid_source(job)
                self.store_unit(asset, 'embedding', ordinal, dict(sample=sample, vector=str(target)))
            elif embedding['sample'] != sample:
                # A recreated preview has a new immutable path, but the vector
                # for this same content/timestamp remains valid.
                self.store_unit(asset, 'embedding', ordinal, dict(sample=sample, vector=embedding['vector']))
            self.done += 1
            self.status()
        self.publish(job, probe['metadata'], frames)
        for ordinal, start in enumerate(speech_times):
            self.checkpoint()
            self.valid_source(job)
            self.phase = 'Transcribing Romanian speech'
            if self.unit(asset, 'speech', ordinal) is None:
                audio = self.unit(asset, 'audio', ordinal)
                if audio is None:
                    target = self.target(asset, '.wav')
                    self.backend.audio(Path(job['path']), target, start)
                    self.valid_source(job)
                    audio = dict(audio=str(target))
                    self.store_unit(asset, 'audio', ordinal, audio)
                self.checkpoint(gpu=True)
                segments = self.backend.speech(Path(audio['audio']), start)
                self.valid_source(job)
                self.store_unit(asset, 'speech', ordinal, dict(path=job['logical'], segments=segments))
            self.done += 1
            self.status()
        with self.db() as db:
            db.execute("UPDATE index_jobs SET state='complete',error=NULL WHERE asset=?", (asset,))
        self.status(force=True, changed=True)

    def work(self):
        attempted = set()
        self.last_scan = time.monotonic()
        while True:
            if (self.scan_requested.is_set() and time.monotonic() - self.last_scan >= 15) or time.monotonic() - self.last_scan >= 60:
                self.scan()
                self.last_scan = time.monotonic()
            with self.db() as db:
                jobs = [dict(r) for r in db.execute("SELECT * FROM index_jobs WHERE state IN ('queued','running') OR (state='error' AND attempts<3) ORDER BY capture_time DESC, rowid DESC")]
            job = next((job for job in jobs if job['asset'] not in attempted), None)
            if job is None:
                break
            self.checkpoint()
            try:
                self.active_job = True
                self.run_job(job)
            except Reprioritize:
                self.scan_requested.set()
                continue
            except Stopped:
                raise
            except Exception as error:
                # Quit can terminate the in-flight FFmpeg child. Preserve the
                # running checkpoint for restart instead of spending a retry.
                if self.stop.is_set():
                    raise Stopped() from error
                with self.db() as db:
                    db.execute("UPDATE index_jobs SET state='error',error=?,attempts=attempts+1 WHERE asset=?", (str(error), job['asset']))
                self.status(error=f"{Path(job['path']).name}: {error}")
            finally:
                self.active_job = False
            attempted.add(job['asset'])
        with self.db() as db:
            errors = db.execute("SELECT count(*) FROM index_jobs WHERE state='error'").fetchone()[0]
        errors += self.scan_errors
        self.phase, self.current = ('Needs attention' if errors else 'Up to date'), ''
        self.done = self.total
        self.status(force=True, error=f'{errors} files need attention. Retry will keep completed checkpoints.' if errors else None)

    def run(self, interval=300):
        while not self.stop.is_set():
            try:
                self.checkpoint()
                self.scan()
                self.work()
            except Stopped:
                return
            except Exception as error:
                self.phase = 'Scan needs attention'
                self.status(error=str(error))
            self.wake.wait(interval)
            self.wake.clear()
