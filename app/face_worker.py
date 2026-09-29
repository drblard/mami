"""Low-priority JSON-lines worker for face detection, grouping and suggestions.

Runs after previews and AI search: import visibility, previews and search
indexing always come first (PIPELINE.md). Reads the catalog and the personal
store; writes only the generated face index.
"""
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
import time

os.environ['OMP_NUM_THREADS'] = '1'

UPSTREAM_POLL_SECONDS = 5
STATUS_INTERVAL_SECONDS = 0.2
# Refresh groups/suggestions during long backfills so review can start early.
REGROUP_EVERY_ASSETS = 250
# An accelerator prediction can hang without returning. No frame for this long
# marks the asset failed and exits so the app restarts a fresh worker.
STALL_SECONDS = 120
STALL_EXIT_STATUS = 70
WATCHDOG_INTERVAL_SECONDS = 5


class Stopped(Exception):
    pass


def catalog_rows(catalog):
    """Assets whose previews are complete, from a read-only catalog connection."""
    with contextlib.closing(sqlite3.connect(Path(catalog).resolve().as_uri() + '?mode=ro', uri=True, timeout=10)) as db:
        present = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
        if not {'index_jobs', 'preview_jobs'} <= present:
            return [], False
        rows = db.execute("SELECT j.asset,j.path,j.kind,j.signature,j.capture_time FROM index_jobs j "
                          "JOIN preview_jobs p ON p.asset=j.asset WHERE p.state='complete'").fetchall()
        upstream = any(db.execute(f'SELECT 1 FROM {view} LIMIT 1').fetchone()
                       for view in ('mami_preview_work', 'mami_index_work') if view in present)
        return rows, upstream


def personal_labels(catalog):
    """People and face labels from the personal store; empty before first use."""
    from user_store import connection
    with connection(catalog) as db:
        present = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'people', 'face_labels'} <= present:
            return {}, []
        people = dict(db.execute('SELECT id,name FROM people').fetchall())
        labels = db.execute('SELECT asset,timestamp,x1,y1,x2,y2,person,verdict FROM face_labels').fetchall()
        return people, labels


class FaceQueue:
    def __init__(self, catalog, faces, extractor_factory, emit, pipeline, clock=time.monotonic, exit=os._exit,
                 stall_seconds=STALL_SECONDS):
        import faces_store
        self.store = faces_store
        self.catalog, self.faces = Path(catalog), Path(faces)
        self.extractor_factory, self.extractor = extractor_factory, None
        self.emit, self.pipeline = emit, pipeline
        self.stop, self.wake, self.paused, self.recompute = (threading.Event() for _ in range(4))
        from gpu_activity import EditorActivity
        self.editor = EditorActivity(self.faces.parent / 'editor-activity.jsonl')
        self.phase, self.current, self.done, self.total = 'Checking faces', '', 0, 0
        self.waiting, self.error, self.last_emit = False, None, 0.0
        self.counts = dict(remaining=0, completed=0, failed=0)
        self.clock, self.exit, self.stall_seconds = clock, exit, stall_seconds
        self.heartbeat_lock = threading.Lock()
        self.active, self.last_heartbeat = None, 0.0
        with self.store.connection(self.faces) as db:
            db.execute("UPDATE face_jobs SET state='queued' WHERE state='running'")
            if db.execute('SELECT paused FROM face_state WHERE id=1').fetchone()[0]:
                self.paused.set()

    def status(self, force=False, changed=False, error=None):
        now = time.monotonic()
        if not (force or changed or error or now - self.last_emit >= STATUS_INTERVAL_SECONDS):
            return
        self.last_emit = now
        with self.store.connection(self.faces) as db:
            self.counts = self.store.counts(db)
        self.emit(dict(phase=self.phase, done=self.done, total=self.total, current=self.current, paused=self.paused.is_set(),
                       busy=False, waiting=self.waiting, changed=changed, error=error, queue_counts=self.counts))

    def command(self, command):
        action = command.get('action')
        if action == 'editor-activity':
            if self.editor.update(command.get('active') is True):
                self.wake.set()
            return
        if action in ('pause', 'resume'):
            paused = action == 'pause'
            self.paused.set() if paused else self.paused.clear()
            with self.store.connection(self.faces) as db:
                db.execute('UPDATE face_state SET paused=? WHERE id=1 AND paused!=?', (int(paused), int(paused)))
        elif action == 'retry':
            with self.store.connection(self.faces) as db:
                db.execute("UPDATE face_jobs SET state='queued',attempts=0,error=NULL WHERE state='error'")
        elif action == 'recompute':
            self.recompute.set()
        elif action == 'stop':
            self.stop.set()
        self.wake.set()
        self.status(force=True)

    def checkpoint(self):
        """Hold while paused or while CapCut is actively used; raise on stop."""
        phase = self.phase
        while self.paused.is_set() or self.editor.active():
            if self.stop.is_set():
                raise Stopped()
            self.waiting = True
            self.phase = 'Paused' if self.paused.is_set() else 'Waiting while CapCut is actively used'
            self.status()
            self.wake.wait(0.5)
            self.wake.clear()
        if self.stop.is_set():
            raise Stopped()
        self.waiting, self.phase = False, phase

    def heartbeat(self):
        with self.heartbeat_lock:
            self.last_heartbeat = self.clock()

    def watchdog_check(self):
        """Returns True (after recording the failure and exiting) if extraction stalled."""
        with self.heartbeat_lock:
            job = self.active
            stalled = job is not None and self.clock() - self.last_heartbeat >= self.stall_seconds
        if not stalled:
            return False
        message = f'Face analysis stalled for {self.stall_seconds} s; the worker was restarted'
        with self.store.connection(self.faces) as db:
            self.store.fail_job(db, job['asset'], message)
        self.status(force=True, error=f"{Path(job['path']).name}: {message}")
        self.exit(STALL_EXIT_STATUS)
        return True

    def watchdog(self):
        while not self.stop.wait(WATCHDOG_INTERVAL_SECONDS):
            if self.watchdog_check():
                return

    def sync(self):
        rows, upstream = catalog_rows(self.catalog)
        with self.store.connection(self.faces) as db:
            self.store.sync_jobs(db, rows, self.pipeline)
        return upstream

    def regroup(self):
        from face_assignments import recompute
        self.phase, self.current = 'Grouping faces', ''
        self.status(force=True)
        people, labels = personal_labels(self.catalog)
        with self.store.connection(self.faces) as db:
            recompute(db, people, labels)
        self.recompute.clear()
        self.status(force=True, changed=True)

    def work(self):
        """Process queued assets; returns (processed count, waiting for upstream work)."""
        processed = 0
        while True:
            self.checkpoint()
            if self.recompute.is_set():
                self.regroup()
            if self.sync():
                return processed, True
            with self.store.connection(self.faces) as db:
                job = self.store.next_job(db)
                if job is not None:
                    job = dict(job)
                    db.execute("UPDATE face_jobs SET state='running' WHERE asset=?", (job['asset'],))
                    self.total = self.store.counts(db)['remaining'] + processed
            if job is None:
                return processed, False
            self.phase, self.current, self.done = 'Finding faces', Path(job['path']).name, processed
            self.status(force=True)
            try:
                if self.extractor is None:
                    self.phase = 'Loading face model'
                    self.status(force=True)
                    self.extractor = self.extractor_factory()
                    self.phase = 'Finding faces'
                with self.heartbeat_lock:
                    self.active, self.last_heartbeat = job, self.clock()
                try:
                    faces = self.extractor.extract(job['asset'], job['path'], job['kind'], cancelled=self.stop.is_set,
                                                   heartbeat=self.heartbeat)
                finally:
                    with self.heartbeat_lock:
                        self.active = None
                with self.store.connection(self.faces) as db:
                    self.store.publish_faces(db, job['asset'], job['signature'], faces)
            except Exception as error:
                stopping = self.stop.is_set()
                with self.store.connection(self.faces) as db:
                    # Quitting interrupts decoding; that is not a failed attempt.
                    if stopping:
                        db.execute("UPDATE face_jobs SET state='queued' WHERE asset=?", (job['asset'],))
                    else:
                        self.store.fail_job(db, job['asset'], error)
                if stopping:
                    raise Stopped() from error
                self.status(error=f"{Path(job['path']).name}: {error}")
            processed += 1
            self.done = processed
            if processed % REGROUP_EVERY_ASSETS == 0:
                self.regroup()

    def run(self, once=False):
        while not self.stop.is_set():
            try:
                self.checkpoint()
                processed, upstream = self.work()
                with self.store.connection(self.faces) as db:
                    stale = self.store.grouping_stale(db)
                if processed or stale or self.recompute.is_set():
                    self.regroup()
                failed = self.counts.get('failed', 0)
                self.current, self.done, self.waiting = '', self.total, upstream
                if upstream:
                    self.phase = 'Waiting for previews and AI search'
                    self.status(force=True)
                else:
                    self.phase = 'Faces need attention' if failed else 'Faces ready'
                    self.status(force=True, error=f'{failed} files need attention. Retry keeps completed faces.' if failed else None)
            except Stopped:
                return
            except Exception as error:
                self.phase = 'Faces need attention'
                self.status(force=True, error=str(error))
            if once:
                return
            self.wake.wait(UPSTREAM_POLL_SECONDS)
            self.wake.clear()


def isolate_protocol():
    """Private line-buffered protocol stream; descriptor 1 then points at stderr.

    Native libraries (CoreML) write diagnostics directly to file descriptor 1,
    which would corrupt the JSON-lines protocol read by the app.
    """
    sys.stdout.flush()
    protocol = os.fdopen(os.dup(1), 'w', buffering=1)
    os.dup2(2, 1)
    return protocol


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--catalog', required=True)
    parser.add_argument('--faces', required=True)
    parser.add_argument('--models', required=True)
    parser.add_argument('--image-helper', required=True)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--cpu', action='store_true', help='Disable CoreML acceleration')
    args = parser.parse_args()
    os.nice(10)
    protocol = isolate_protocol()
    lock = threading.Lock()
    def emit(event):
        with lock:
            print(json.dumps(event, ensure_ascii=False), file=protocol, flush=True)
    faces = Path(args.faces)
    faces.parent.mkdir(parents=True, exist_ok=True)
    with (faces.parent / 'faces.worker.lock').open('a+b') as owner:
        try:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            emit(dict(phase='Another Mami instance is finding faces', done=0, total=0, current='', paused=False, busy=False,
                      changed=False, error='Close the other Mami instance before restarting this worker.'))
            return
        with contextlib.redirect_stdout(sys.stderr):
            from app_paths import media_tool
            from model_config import FACE_MODEL, FACE_PIPELINE
            def extractor():
                import face_engine
                from face_index import DETECTION_SIDES, FaceExtractor
                engine = face_engine.FaceEngine(args.models, faces.parent / 'Engine', FACE_MODEL, DETECTION_SIDES, accelerate=not args.cpu)
                return FaceExtractor(engine, args.image_helper, media_tool('ffmpeg'), media_tool('ffprobe'), faces.parent / 'Crops')
            queue = FaceQueue(args.catalog, faces, extractor, emit, FACE_PIPELINE)
            signal.signal(signal.SIGTERM, lambda *_: (queue.stop.set(), queue.wake.set()))
            if args.once:
                queue.run(once=True)
                return
            def controls():
                try:
                    for line in sys.stdin:
                        queue.command(json.loads(line))
                except Exception as error:
                    queue.status(error=str(error))
                finally:
                    queue.stop.set()
                    queue.wake.set()
            threading.Thread(target=controls, daemon=True).start()
            threading.Thread(target=queue.watchdog, daemon=True).start()
            queue.run()


if __name__ == '__main__':
    main()
