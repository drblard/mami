"""Independent, resumable thumbnail/coarse-scrub/dense-preview scheduling."""
import json
import contextlib
from pathlib import Path
import time
from index_store import THUMBNAIL_STAGE, COARSE_STAGE, DENSE_STAGE, COMPLETE_STAGE, PENDING_PREVIEW_PREDICATE
from model_config import PIPELINE

COARSE_PREVIEW_SAMPLES = 12
DENSE_PREVIEW_BATCH = 16


def coarse_ordinals(count):
    if count <= COARSE_PREVIEW_SAMPLES:
        return list(range(count))
    return [round(index*(count-1)/(COARSE_PREVIEW_SAMPLES-1)) for index in range(COARSE_PREVIEW_SAMPLES)]


class PreviewPipeline:
    def __init__(self, worker):
        self.worker = worker

    def frames(self, asset, count):
        with self.worker.db() as db:
            rows = db.execute("SELECT ordinal,payload FROM index_units WHERE asset=? AND pipeline=? AND stage='frame' ORDER BY ordinal", (asset, PIPELINE)).fetchall()
        result = {}
        for ordinal, payload in rows:
            sample = json.loads(payload)
            if 0 <= ordinal < count and Path(sample['frame']).is_file():
                result[ordinal] = sample
        return result

    def step(self, job):
        worker = self.worker
        previous = worker.processing_preview
        worker.processing_preview = True
        try:
            worker.checkpoint()
            worker.valid_source(job)
            with worker.db() as db:
                row = db.execute('SELECT stage,state FROM preview_jobs WHERE asset=?', (job['asset'],)).fetchone()
                if row is None or row['state'] == 'complete':
                    return True
                stage = row['stage']
                db.execute("UPDATE preview_jobs SET state='running',error=NULL WHERE asset=?", (job['asset'],))
            worker.phase, worker.current = 'Reading preview metadata', Path(job['path']).name
            probe = worker.read_probe(job)
            timestamps = probe['timestamps']
            if not timestamps:
                raise ValueError('Media has no preview timestamps')
            frames = self.frames(job['asset'], len(timestamps))
            if stage == THUMBNAIL_STAGE:
                requested = [0]
                worker.phase = 'Creating thumbnails'
            elif stage == COARSE_STAGE:
                requested = coarse_ordinals(len(timestamps))
                worker.phase = 'Preparing scrub previews'
            else:
                requested = [i for i in range(len(timestamps)) if i not in frames][:DENSE_PREVIEW_BATCH]
                worker.phase = 'Refining scrub previews'
            worker.done, worker.total = len(frames), len(timestamps)
            worker.status(force=True)
            missing = [ordinal for ordinal in requested if ordinal not in frames]
            targets = {ordinal: worker.target(job['asset'], '.jpg') for ordinal in missing}
            if missing and hasattr(worker.backend, 'frames') and callable(worker.backend.frames):
                worker.checkpoint()
                worker.valid_source(job)
                worker.backend.frames(Path(job['path']), [(targets[i], timestamps[i]) for i in missing])
                worker.valid_source(job)
            for ordinal in requested:
                worker.checkpoint()
                if ordinal in frames:
                    continue
                worker.valid_source(job)
                target = targets[ordinal]
                if not target.exists():
                    worker.backend.frame(Path(job['path']), target, timestamps[ordinal])
                worker.valid_source(job)
                sample = dict(path=job['logical'], kind=job['kind'], timestamp=timestamps[ordinal], frame=str(target))
                worker.store_unit(job['asset'], 'frame', ordinal, sample)
                frames[ordinal] = sample
                worker.done = len(frames)
                worker.status()
            complete = len(frames) == len(timestamps)
            worker.publish(job, probe['metadata'], [frames[i] for i in sorted(frames)],
                           preview_state='ready' if complete else 'partial')
            with worker.db() as db:
                db.execute('UPDATE preview_control SET dispatch_order=dispatch_order+1 WHERE id=1')
                dispatch_order = db.execute('SELECT dispatch_order FROM preview_control WHERE id=1').fetchone()[0]
                db.execute('UPDATE preview_jobs SET state=?,stage=?,last_served=?,error=NULL WHERE asset=?',
                           ('complete' if complete else 'queued', COMPLETE_STAGE if complete else min(stage+1, DENSE_STAGE),
                            0 if stage == THUMBNAIL_STAGE and not complete else dispatch_order, job['asset']))
            worker.status(force=True, changed=True)
            return complete
        finally:
            worker.processing_preview = previous

    def prepare(self, job):
        """Standalone/once-mode convenience; GUI processes use their own queue."""
        while not self.step(job):
            pass

    def work(self):
        from index_queue import Stopped
        worker = self.worker
        failed = set()
        while True:
            worker.checkpoint()
            if worker.role == 'all' and worker.scan_requested.is_set() and time.monotonic()-worker.last_scan >= 15:
                worker.scan()
            with worker.db() as db:
                with contextlib.closing(db.execute('SELECT asset FROM preview_jobs WHERE '+PENDING_PREVIEW_PREDICATE+' ORDER BY priority DESC,stage,last_served,capture_time DESC,asset')) as candidates:
                    asset = next((row['asset'] for row in candidates if row['asset'] not in failed), None)
                row = db.execute('SELECT * FROM index_jobs WHERE asset=?', (asset,)).fetchone() if asset else None
                job = dict(row) if row else None
            if job is None:
                break
            try:
                self.step(job)
            except Stopped:
                raise
            except Exception as error:
                if worker.stop.is_set():
                    raise Stopped() from error
                with worker.db() as db:
                    current = db.execute('SELECT signature FROM index_jobs WHERE asset=?', (job['asset'],)).fetchone()
                    # A verified import can update a hard-link/ctime signature
                    # while a preview finishes. Do not overwrite that new request.
                    if current is not None and current['signature'] != job['signature']:
                        continue
                    db.execute("UPDATE preview_jobs SET state='error',error=?,attempts=attempts+1 WHERE asset=?", (str(error), job['asset']))
                failed.add(job['asset'])
                worker.status(force=True, error=f"{Path(job['path']).name}: {error}")
        with worker.db() as db:
            errors = db.execute("SELECT count(*) FROM preview_jobs WHERE state='error'").fetchone()[0]
        final_phase = 'Preview needs attention' if errors else 'Previews ready'
        changed_phase = worker.phase != final_phase or bool(worker.current)
        worker.phase, worker.current = final_phase, ''
        worker.done = worker.total
        if changed_phase:
            worker.status(force=True, error=f'{errors} previews need attention' if errors else None)
