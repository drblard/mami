"""Durable, cooperative background scan/index queue. Originals are read-only.

Each successful file hash, frame, embedding and 30-second speech chunk commits
independently. Artifacts are fsynced before SQLite references them. Interrupted
artifacts are retained; only the unfinished unit is retried after a crash.
"""
import contextlib
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid
from user_store import connection as user_connection
from model_config import PIPELINE
from index_store import connection, ensure_schema, signature, MEDIA_EXTENSIONS, MAX_JOB_ATTEMPTS, PENDING_PREVIEW_PREDICATE

EXTENSIONS = MEDIA_EXTENSIONS
# Re-verifying ~140k stored frame/vector files takes seconds; missing files are
# rare (manual deletion), so the audit runs at most hourly, not on every scan.
ARTIFACT_AUDIT_INTERVAL_SECONDS = 3600


class Stopped(Exception):
    pass

class Reprioritize(Exception):
    pass

class GPUDeferred(Exception):
    """Keep saved visual work and let CPU-only jobs pass a busy GPU."""
    pass


class PreviewsPending(Exception):
    """Inference waits for independently generated previews."""

class Queue:
    def __init__(self, database, root, artifacts, backend, emit=lambda event: None, gpu_wait=None, role='all'):
        if role not in ('all', 'index', 'preview'):
            raise ValueError('Unknown worker role')
        self.role = role
        self.control_table = 'preview_control' if role == 'preview' else 'scan_control'
        self.jobs_table = 'preview_jobs' if role == 'preview' else 'index_jobs'
        self.processing_preview = False
        self.database, self.root, self.artifacts = map(Path, (database, root, artifacts))
        self.backend, self.emit = backend, emit
        self.gpu_wait = gpu_wait
        from gpu_activity import EditorActivity
        self.editor_activity = EditorActivity(self.database.parent / 'editor-activity.jsonl')
        self.gpu_utilization = None
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.busy = threading.Event()
        self.gpu_busy = threading.Event()
        self.paused = threading.Event()
        self.phase, self.done, self.total, self.current = 'Waiting', 0, 0, ''
        self.last_emit = 0
        self.last_queue_count = 0
        self.queue_counts = dict(remaining=0, completed=0, failed=0)
        self.waiting = False
        self.scan_errors = 0
        self.scan_requested = threading.Event()
        self.last_scan = time.monotonic()
        self.last_artifact_audit = float('-inf')
        self.active_job = False
        self.allow_gpu_defer = False
        self.initialize()

    @contextlib.contextmanager
    def db(self):
        with connection(self.database) as db:
            yield db

    def initialize(self):
        with self.db() as db:
            ensure_schema(db)
            if db.execute(f'SELECT paused FROM {self.control_table} WHERE id=1').fetchone()[0]:
                self.paused.set()
            # Each process recovers only the queue whose ownership lock it holds.
            if self.role in ('all', 'index'):
                db.execute("UPDATE index_jobs SET state='queued' WHERE state='running'")
            if self.role in ('all', 'preview'):
                db.execute("UPDATE preview_jobs SET state='queued' WHERE state='running'")

    def status(self, force=False, changed=False, error=None):
        now = time.monotonic()
        if force or changed or error or now - self.last_emit >= .2:
            self.last_emit = now
            if force or now - self.last_queue_count >= 2:
                with self.db() as db:
                    counts = dict(db.execute(f'SELECT state,count(*) FROM {self.jobs_table} GROUP BY state').fetchall())
                self.queue_counts = dict(remaining=sum(value for state, value in counts.items() if state != 'complete'),
                                         completed=counts.get('complete', 0), failed=counts.get('error', 0))
                self.last_queue_count = now
            self.emit(dict(phase=self.phase, done=self.done, total=self.total, current=self.current,
                           paused=self.paused.is_set(), busy=self.busy.is_set(), waiting=self.waiting, gpu_utilization=self.gpu_utilization, changed=changed, error=error,
                           queue_counts=self.queue_counts))

    def command(self, command):
        action = command.get('action')
        if action == 'editor-activity':
            if self.editor_activity.update(command.get('active') is True):
                self.wake.set()
            return
        if action in ('pause', 'resume'):
            paused = action == 'pause'
            # Set the in-memory flag immediately; persist before acknowledging.
            self.paused.set() if paused else self.paused.clear()
            with self.db() as db:
                db.execute(f'UPDATE {self.control_table} SET paused=? WHERE id=1 AND paused != ?', (int(paused), int(paused)))
        elif action == 'busy':
            self.busy.set() if command.get('value') else self.busy.clear()
        elif action == 'gpu-busy':
            self.gpu_busy.set() if command.get('value') else self.gpu_busy.clear()
        elif action == 'retry':
            with self.db() as db:
                db.execute(f"UPDATE {self.jobs_table} SET state='queued',attempts=0,error=NULL WHERE state='error'")
        elif action == 'stop':
            self.stop.set()
        if action in ('scan', 'retry') and self.role != 'preview':
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
        if self.active_job and not self.processing_preview and self.pending_previews():
            raise PreviewsPending()
        if self.active_job and not self.processing_preview and ((self.scan_requested.is_set() and time.monotonic() - self.last_scan >= 15) or time.monotonic() - self.last_scan >= 60):
            raise Reprioritize()
        if gpu and self.gpu_wait:
            self.gpu_wait(self)
            self.checkpoint()
        self.waiting = False
        self.phase = phase

    @staticmethod
    def signature(path):
        return signature(path)

    def pending_previews(self):
        if self.role == 'preview' or self.processing_preview:
            return False
        with self.db() as db:
            if db.execute('SELECT paused FROM preview_control WHERE id=1').fetchone()[0]:
                return False
            return db.execute('SELECT 1 FROM preview_jobs WHERE '+PENDING_PREVIEW_PREDICATE+' LIMIT 1').fetchone() is not None

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
        self.last_scan = time.monotonic()
        self.scan_requested.clear()
        self.scan_errors = 0
        self.phase, self.done, self.total, self.current = 'Discovering files', 0, 0, ''
        self.status(force=True)
        roots = {self.root.resolve()}
        with user_connection(self.database) as db:
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
        # One read of what is already known lets unchanged files be recognized
        # without a write transaction each; only new or changed files take the
        # full path below.
        with self.db() as db:
            saved_files = {row['path']: (row['signature'], row['asset']) for row in db.execute('SELECT path,signature,asset FROM scan_files')}
            known_paths = {row['asset']: row['path'] for row in db.execute('SELECT asset,path FROM media')}
            known_jobs = {row['asset']: dict(row) for row in db.execute('SELECT asset,path,signature,state,capture_time FROM index_jobs')}
        for path in sorted(files):
            self.checkpoint()
            self.current = path.name
            try:
                signature = self.signature(path)
                saved_file = saved_files.get(str(path))
                if saved_file and saved_file[0] == signature and self.unchanged(str(path), signature, saved_file[1], known_paths, known_jobs, imported_times):
                    self.done += 1
                    self.status()
                    continue
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
                        before = db.total_changes
                        db.execute('UPDATE index_jobs SET path=?,signature=? WHERE asset=? AND (path != ? OR signature != ?)', (str(path), signature, asset, str(path), signature))
                        if db.total_changes != before:
                            # The content was re-verified under its new signature (metadata-only
                            # changes such as xattrs or hard links alter ctime). Failures against
                            # the old signature are obsolete; completed work is kept.
                            db.execute("UPDATE preview_jobs SET state='queued',attempts=0,error=NULL WHERE asset=? AND state='error'", (asset,))
                            db.execute("UPDATE index_jobs SET state='queued',attempts=0,error=NULL WHERE asset=? AND state='error'", (asset,))
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
        if time.monotonic() - self.last_artifact_audit >= ARTIFACT_AUDIT_INTERVAL_SECONDS:
            self.repair_missing_artifacts()
            self.last_artifact_audit = time.monotonic()
        version = getattr(self.backend, 'metadata_version', 0)
        if version:
            with self.db() as db:
                for row in db.execute("SELECT asset,payload FROM index_units WHERE pipeline=? AND stage='metadata' AND ordinal=0", (PIPELINE,)).fetchall():
                    if json.loads(row['payload']).get('metadataVersion', 0) < version:
                        db.execute("UPDATE index_jobs SET state='queued',attempts=0,error=NULL WHERE asset=? AND state IN ('complete','error')", (row['asset'],))

    @staticmethod
    def unchanged(path, signature, asset, known_paths, known_jobs, imported_times):
        """True when the full scan step would change nothing for this file."""
        known_path, job = known_paths.get(asset), known_jobs.get(asset)
        if known_path is not None and known_path != path:
            return False
        if job is None:
            return known_path == path  # Seeded media without a queue job.
        if job['path'] != path or job['signature'] != signature:
            return False
        imported = imported_times.get(asset)
        return job['state'] == 'complete' or (job['capture_time'] is not None and (imported is None or job['capture_time'] == imported))

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
                db.executemany("UPDATE preview_jobs SET state='queued',stage=0,attempts=0,error=NULL WHERE asset=?", [(asset,) for asset in damaged])

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

    def publish(self, job, metadata, frames, preview_state='ready'):
        self.valid_source(job)
        value = dict(path=job['logical'], kind=job['kind'], url=Path(job['path']).as_uri(), frames=frames,
                      match=frames[0], metadata=metadata, assetID=job['asset'], previewState=preview_state)
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
            with user_connection(self.database) as db:
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
        if self.role == 'all':
            from preview_pipeline import PreviewPipeline
            PreviewPipeline(self).prepare(job)
        self.phase, self.current, self.done, self.total = 'Reading metadata', Path(job['path']).name, 0, 0
        self.status(force=True)
        self.checkpoint()
        probe = self.read_probe(job)
        timestamps = probe['timestamps']
        speech_times = probe['speech_times']
        self.total = len(timestamps) + len(speech_times)
        self.done = 0
        frames = []
        for ordinal, timestamp in enumerate(timestamps):
            self.checkpoint()
            self.valid_source(job)
            sample = self.unit(asset, 'frame', ordinal)
            if sample is None:
                with self.db() as db:
                    db.execute("UPDATE preview_jobs SET state='queued',stage=0 WHERE asset=?", (asset,))
                raise PreviewsPending()
            frames.append(sample)
            self.phase = 'Indexing visual content'
            embedding = self.unit(asset, 'embedding', ordinal)
            if embedding is None:
                target = self.target(asset, '.npy')
                if sample.get('crop') is not None:
                    self.backend.embedding(Path(sample['frame']), target, crop=sample['crop'])
                else:
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
        if self.role in ('all', 'preview'):
            from preview_pipeline import PreviewPipeline
            PreviewPipeline(self).work()
            if self.role == 'preview':
                return
        if self.pending_previews():
            self.phase = 'Waiting for previews'
            self.waiting = True
            self.status(force=True)
            return
        attempted = set()
        deferred = set()
        retry_gpu_at = time.monotonic() + 30
        while True:
            if self.pending_previews():
                self.phase = 'Waiting for previews'
                self.status(force=True)
                return
            if time.monotonic() >= retry_gpu_at:
                attempted.difference_update(deferred)
                retry_gpu_at = time.monotonic() + 30
            if (self.scan_requested.is_set() and time.monotonic() - self.last_scan >= 15) or time.monotonic() - self.last_scan >= 60:
                self.scan()
                self.last_scan = time.monotonic()
            with self.db() as db:
                jobs = [dict(r) for r in db.execute("SELECT j.* FROM index_jobs j JOIN preview_jobs p ON p.asset=j.asset WHERE p.state='complete' AND (j.state IN ('queued','running') OR (j.state='error' AND j.attempts<?)) ORDER BY j.capture_time DESC,j.rowid DESC",(MAX_JOB_ATTEMPTS,))]
            job = next((job for job in jobs if job['asset'] not in attempted), None)
            self.allow_gpu_defer = job is not None
            if job is None:
                job = next((job for job in jobs if job['asset'] in deferred), None)
            if job is None:
                break
            self.checkpoint()
            try:
                self.active_job = True
                self.run_job(job)
                deferred.discard(job['asset'])
            except GPUDeferred:
                deferred.add(job['asset'])
                self.phase = 'Speech yielding to active editing — continuing visual indexing'
                self.status(force=True)
            except Reprioritize:
                self.scan_requested.set()
                continue
            except PreviewsPending:
                with self.db() as db:
                    db.execute("UPDATE index_jobs SET state='queued' WHERE asset=?", (job['asset'],))
                self.phase = 'Waiting for previews'
                self.status(force=True)
                return
            except Stopped:
                raise
            except Exception as error:
                # Quit can terminate the in-flight FFmpeg child. Preserve the
                # running checkpoint for restart instead of spending a retry.
                if self.stop.is_set():
                    raise Stopped() from error
                with self.db() as db:
                    db.execute("UPDATE index_jobs SET state='error',error=?,attempts=attempts+1 WHERE asset=?", (str(error), job['asset']))
                deferred.discard(job['asset'])
                self.status(error=f"{Path(job['path']).name}: {error}")
            finally:
                self.active_job = False
            attempted.add(job['asset'])
        with self.db() as db:
            errors = db.execute("SELECT count(*) FROM index_jobs WHERE state='error'").fetchone()[0]
            blocked = db.execute("SELECT 1 FROM index_jobs j JOIN preview_jobs p ON p.asset=j.asset WHERE j.state!='complete' AND p.state!='complete' LIMIT 1").fetchone()
        errors += self.scan_errors
        self.phase, self.current = ('Needs attention' if errors else 'Waiting for previews' if blocked else 'Up to date'), ''
        self.done = self.total
        self.status(force=True, error=f'{errors} files need attention. Retry will keep completed checkpoints.' if errors else None)

    def run(self, interval=300):
        first = True
        while not self.stop.is_set():
            try:
                self.checkpoint()
                if self.role != 'preview' and (first or self.scan_requested.is_set() or time.monotonic()-self.last_scan >= interval):
                    self.scan()
                first = False
                self.work()
            except Stopped:
                return
            except Exception as error:
                self.phase = 'Scan needs attention'
                self.status(error=str(error))
            self.wake.wait(1 if self.role == 'preview' or self.pending_previews() else interval)
            self.wake.clear()
