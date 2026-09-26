import json
import contextlib
from pathlib import Path
import sqlite3
import tempfile
import threading
import subprocess
import sys
import unittest
from unittest.mock import patch

from index_queue import Queue, Stopped


class Backend:
    def __init__(self):
        self.frames, self.vectors, self.speech_calls = [], [], []
        self.interrupt = None
        self.interrupt_speech = None

    def probe(self, source, kind):
        return dict(metadata=None, timestamps=[.5, 1.5, 2.5], speech_times=[0, 30])

    def frame(self, source, target, timestamp):
        if self.interrupt == timestamp:
            raise Stopped()
        target.write_bytes(b'frame')
        self.frames.append(timestamp)

    def embedding(self, source, target):
        target.write_bytes(b'vector')
        self.vectors.append(str(source))

    def audio(self, source, target, start):
        target.write_bytes(b'audio')

    def speech(self, source, start):
        if self.interrupt_speech == start:
            raise Stopped()
        self.speech_calls.append(start)
        return [dict(start=start, end=start + 1, text='Bună ziua')]


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'originals'
        self.root.mkdir()
        self.source = self.root / 'a.mp4'
        self.source.write_bytes(b'original')
        self.database = self.base / 'catalog.sqlite'
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            db.executescript('CREATE TABLE state(id INTEGER PRIMARY KEY, identity TEXT, revision INTEGER, change_token TEXT); INSERT INTO state VALUES(1,"test",0,"initial"); CREATE TABLE media(path TEXT PRIMARY KEY, asset TEXT, payload TEXT);')
        self.backend = Backend()

    def queue(self, emit=lambda event: None):
        return Queue(self.database, self.root, self.base / 'artifacts', self.backend, emit)

    def test_custom_and_offline_roots(self):
        custom = self.base / 'custom'
        custom.mkdir()
        (custom / 'b.mov').write_bytes(b'cloud original')
        q = self.queue()
        with q.db() as db:
            db.execute('CREATE TABLE media_roots(path TEXT PRIMARY KEY)')
            for root in (custom, self.root, self.root / 'nested', self.base / 'offline'):
                db.execute('INSERT INTO media_roots VALUES(?)', (str(root),))
        q.scan()
        with q.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM index_jobs').fetchone()[0], 2)
        custom.rename(self.base / 'disconnected')
        q.scan()
        with q.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM index_jobs').fetchone()[0], 2)

    def test_crash_reuses_frames_embeddings_and_speech_chunks(self):
        q = self.queue()
        q.scan()
        self.backend.interrupt = 1.5
        with self.assertRaises(Stopped):
            q.work()
        self.assertEqual(self.backend.frames, [.5])
        self.assertEqual(len(self.backend.vectors), 1)
        self.backend.interrupt = None
        # Simulate process restart. The running job is recovered, not discarded.
        restarted = self.queue()
        restarted.work()
        self.assertEqual(self.backend.frames, [.5, 1.5, 2.5])
        self.assertEqual(len(self.backend.vectors), 3)
        self.assertEqual(self.backend.speech_calls, [0, 30])
        restarted.work()
        self.assertEqual(self.backend.speech_calls, [0, 30])
        with restarted.db() as db:
            self.assertEqual(db.execute('SELECT state FROM index_jobs').fetchone()[0], 'complete')

    def test_process_exit_recovers_committed_units(self):
        script = """
import os, sys
from test_index_queue import Backend
from index_queue import Queue
class Crash(Backend):
    def frame(self, source, target, timestamp):
        if timestamp == 1.5: os._exit(77)
        super().frame(source, target, timestamp)
q = Queue(sys.argv[1], sys.argv[2], sys.argv[3], Crash())
q.scan()
q.work()
"""
        result = subprocess.run([sys.executable, '-c', script, str(self.database), str(self.root), str(self.base / 'artifacts')], cwd=Path(__file__).parent)
        self.assertEqual(result.returncode, 77)
        q = self.queue()
        q.work()
        self.assertEqual(self.backend.frames, [1.5, 2.5])
        self.assertEqual(len(self.backend.vectors), 2)
        with q.db() as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(db.execute('SELECT state FROM index_jobs').fetchone()[0], 'complete')

    def test_unchanged_scan_ignores_lrf_and_does_not_write(self):
        (self.root / 'a.LRF').write_bytes(b'proxy')
        q = self.queue()
        q.scan()
        q.work()
        with q.db() as db:
            revision = db.execute('SELECT revision FROM state').fetchone()[0]
            self.assertEqual(db.execute('SELECT count(*) FROM scan_files').fetchone()[0], 1)
        with patch.object(q, 'fingerprint', side_effect=AssertionError('Unchanged file rehashed')):
            q.scan()
            q.work()
        with q.db() as db:
            self.assertEqual(db.execute('SELECT revision FROM state').fetchone()[0], revision)

    def test_completed_job_repairs_preview_without_repeating_inference(self):
        q = self.queue()
        q.scan()
        q.work()
        with q.db() as db:
            asset = db.execute('SELECT asset FROM index_jobs').fetchone()[0]
        old = q.unit(asset, 'frame', 0)['frame']
        Path(old).unlink()  # Isolated Linux test fixture, never a Mac artifact.
        q.scan()
        q.work()
        repaired = q.unit(asset, 'frame', 0)['frame']
        self.assertNotEqual(old, repaired)
        self.assertTrue(Path(repaired).is_file())
        self.assertEqual(q.unit(asset, 'embedding', 0)['sample']['frame'], repaired)
        self.assertEqual(len(self.backend.frames), 4)
        self.assertEqual(len(self.backend.vectors), 3)
        self.assertEqual(self.backend.speech_calls, [0, 30])

    def test_completed_job_repairs_only_missing_vector(self):
        q = self.queue()
        q.scan()
        q.work()
        with q.db() as db:
            asset = db.execute('SELECT asset FROM index_jobs').fetchone()[0]
        Path(q.unit(asset, 'embedding', 1)['vector']).unlink()
        q.scan()
        q.work()
        self.assertEqual(len(self.backend.frames), 3)
        self.assertEqual(len(self.backend.vectors), 4)
        self.assertEqual(self.backend.speech_calls, [0, 30])

    def test_crash_between_speech_chunks_keeps_completed_transcript(self):
        q = self.queue()
        q.scan()
        self.backend.interrupt_speech = 30
        with self.assertRaises(Stopped):
            q.work()
        self.assertEqual(self.backend.speech_calls, [0])
        self.backend.interrupt_speech = None
        self.queue().work()
        self.assertEqual(self.backend.speech_calls, [0, 30])
        self.assertEqual(len(self.backend.frames), 3)

    def test_pause_persists_and_resume_unblocks(self):
        q = self.queue()
        q.command({'action': 'pause'})
        waiting = threading.Event()
        restarted = self.queue(lambda event: waiting.set() if event.get('waiting') else None)
        self.assertTrue(restarted.paused.is_set())
        finished = threading.Event()
        thread = threading.Thread(target=lambda: (restarted.checkpoint(), finished.set()))
        thread.start()
        self.assertTrue(waiting.wait(2))
        self.assertFalse(finished.is_set())
        restarted.command({'action': 'resume'})
        self.assertTrue(finished.wait(2))
        thread.join()
        self.assertFalse(self.queue().paused.is_set())

    def test_gpu_busy_blocks_gpu_boundary_but_not_cpu_scan(self):
        waiting, finished = threading.Event(), threading.Event()
        q = self.queue(lambda event: waiting.set() if event.get('phase') == 'Waiting for GPU' else None)
        q.command({'action': 'gpu-busy', 'value': True})
        q.scan()
        thread = threading.Thread(target=lambda: (q.checkpoint(gpu=True), finished.set()))
        thread.start()
        self.assertTrue(waiting.wait(2))
        self.assertFalse(finished.is_set())
        q.command({'action': 'gpu-busy', 'value': False})
        self.assertTrue(finished.wait(2))
        thread.join()

    def test_changed_source_does_not_publish_old_job(self):
        q = self.queue()
        q.scan()
        self.source.write_bytes(b'changed original')
        q.work()
        with q.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM media').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT state FROM index_jobs').fetchone()[0], 'error')
        self.assertEqual(self.backend.frames, [])

    def test_automatic_rescan_discovers_new_file(self):
        first, second = threading.Event(), threading.Event()
        count = [0]
        def event(value):
            if value['phase'] == 'Up to date':
                count[0] += 1
                (first if count[0] == 1 else second).set()
        q = self.queue(event)
        thread = threading.Thread(target=lambda: q.run(interval=.2))
        thread.start()
        try:
            self.assertTrue(first.wait(3))
            (self.root / 'b.mp4').write_bytes(b'new footage')
            self.assertTrue(second.wait(3))
            with q.db() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM index_jobs WHERE state='complete'").fetchone()[0], 2)
        finally:
            q.stop.set()
            q.wake.set()
            thread.join(3)

    def test_move_keeps_content_identity_and_cached_results(self):
        q = self.queue()
        q.scan()
        q.work()
        renamed = self.root / 'renamed.mp4'
        self.source.rename(renamed)
        q.scan()
        q.work()
        with q.db() as db:
            rows = db.execute('SELECT path,payload FROM media').fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['path'], str(renamed))
        self.assertEqual(json.loads(rows[0]['payload'])['url'], renamed.as_uri())
        self.assertEqual(len(self.backend.vectors), 3)


if __name__ == '__main__':
    unittest.main()
