import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

try:
    import numpy as np
    import face_worker
    import faces_store
except ImportError:
    np = None


class FakeExtractor:
    def __init__(self, fail=(), block=None):
        self.fail, self.block, self.calls = set(fail), block, []

    def extract(self, asset, path, kind, cancelled=lambda: False, heartbeat=lambda: None):
        self.calls.append(asset)
        if self.block is not None:
            self.block.wait(5)
            if cancelled():
                raise InterruptedError('cancelled')
        if asset in self.fail:
            raise RuntimeError('cannot decode')
        vector = np.zeros(512, np.float32); vector[len(self.calls) % 512] = 1
        return [dict(timestamp=None, box=(0.1, 0.1, 0.3, 0.4), score=0.9, eye_distance=40, frontalness=0.9, sharpness=1,
                     norm=20, track=0, representative=True, reliable=True, embedding=vector)]


@unittest.skipIf(np is None, 'NumPy face checks run where the worker runtime is installed')
class FaceWorkerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.catalog, self.faces = root / 'Catalog' / 'catalog.sqlite', root / 'Faces' / 'faces.sqlite'
        self.catalog.parent.mkdir()
        with sqlite3.connect(self.catalog) as db:
            db.executescript('''
                CREATE TABLE state(id INTEGER PRIMARY KEY, identity TEXT, revision INTEGER, change_token TEXT);
                INSERT INTO state VALUES(1,'library',0,'t');
                CREATE TABLE index_jobs(asset TEXT PRIMARY KEY, path TEXT, kind TEXT, signature TEXT, state TEXT, capture_time REAL);
                CREATE TABLE preview_jobs(asset TEXT PRIMARY KEY, state TEXT);
                CREATE VIEW mami_preview_work AS SELECT asset FROM preview_jobs WHERE state='queued';
                CREATE VIEW mami_index_work AS SELECT asset FROM index_jobs WHERE state='queued';
            ''')
            for number, asset in enumerate(('a', 'b', 'c')):
                db.execute("INSERT INTO index_jobs VALUES(?,?,?,?,'complete',?)", (asset, f'/m/{asset}.jpg', 'image', 's', number))
                db.execute("INSERT INTO preview_jobs VALUES(?,'complete')", (asset,))
        self.events = []

    def tearDown(self):
        self.directory.cleanup()

    def queue(self, extractor):
        return face_worker.FaceQueue(self.catalog, self.faces, lambda: extractor, self.events.append, 'p1')

    def test_processes_newest_first_then_groups_and_reports_ready(self):
        extractor = FakeExtractor()
        self.queue(extractor).run(once=True)
        self.assertEqual(extractor.calls, ['c', 'b', 'a'])
        self.assertEqual(self.events[-1]['phase'], 'Faces ready')
        self.assertEqual(self.events[-1]['queue_counts'], dict(remaining=0, completed=3, failed=0))
        with faces_store.connection(self.faces) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM face_groups').fetchone()[0], 3)
            self.assertFalse(faces_store.grouping_stale(db))

    def test_waits_for_previews_and_ai_search_without_extracting(self):
        with sqlite3.connect(self.catalog) as db:
            db.execute("INSERT INTO index_jobs VALUES('d','/m/d.mov','video','s','queued',9)")
            db.execute("INSERT INTO preview_jobs VALUES('d','complete')")
        extractor = FakeExtractor()
        self.queue(extractor).run(once=True)
        self.assertEqual(extractor.calls, [])
        self.assertEqual(self.events[-1]['phase'], 'Waiting for previews and AI search')
        self.assertTrue(self.events[-1]['waiting'])

    def test_failures_are_reported_and_retry_requeues_them(self):
        extractor = FakeExtractor(fail={'b'})
        queue = self.queue(extractor)
        for _ in range(faces_store.MAX_FACE_JOB_ATTEMPTS):
            queue.run(once=True)
        self.assertEqual(extractor.calls.count('b'), faces_store.MAX_FACE_JOB_ATTEMPTS)
        self.assertEqual(self.events[-1]['phase'], 'Faces need attention')
        self.assertEqual(self.events[-1]['queue_counts']['failed'], 1)
        extractor.fail.clear()
        queue.command(dict(action='retry'))
        queue.run(once=True)
        self.assertEqual(self.events[-1]['phase'], 'Faces ready')
        self.assertEqual(self.events[-1]['queue_counts'], dict(remaining=0, completed=3, failed=0))

    def test_pause_is_persisted_across_worker_restarts(self):
        queue = self.queue(FakeExtractor())
        queue.command(dict(action='pause'))
        restarted = self.queue(FakeExtractor())
        self.assertTrue(restarted.paused.is_set())
        restarted.command(dict(action='resume'))
        self.assertFalse(self.queue(FakeExtractor()).paused.is_set())

    def test_stalled_extraction_is_failed_and_the_worker_exits(self):
        now = [100.0]
        exits = []
        release = threading.Event()
        extractor = FakeExtractor(block=release)
        queue = face_worker.FaceQueue(self.catalog, self.faces, lambda: extractor, self.events.append, 'p1',
                                      clock=lambda: now[0], exit=exits.append, stall_seconds=120)
        worker = threading.Thread(target=queue.run, kwargs=dict(once=True))
        worker.start()
        while not extractor.calls:
            threading.Event().wait(0.01)
        now[0] += 119
        self.assertFalse(queue.watchdog_check())
        queue.heartbeat(); now[0] += 119
        self.assertFalse(queue.watchdog_check())
        now[0] += 1
        self.assertTrue(queue.watchdog_check())
        self.assertEqual(exits, [face_worker.STALL_EXIT_STATUS])
        with faces_store.connection(self.faces) as db:
            row = db.execute('SELECT state,attempts,error FROM face_jobs WHERE asset=?', (extractor.calls[0],)).fetchone()
        self.assertEqual((row['state'], row['attempts']), ('error', 1))
        self.assertIn('stalled for 120 s', row['error'])
        queue.stop.set(); release.set(); worker.join(5)
        self.assertFalse(worker.is_alive())

    def test_stop_during_extraction_requeues_instead_of_failing(self):
        release = threading.Event()
        extractor = FakeExtractor(block=release)
        queue = self.queue(extractor)
        worker = threading.Thread(target=queue.run)
        worker.start()
        while not extractor.calls:
            threading.Event().wait(0.01)
        queue.command(dict(action='stop'))
        release.set()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        with faces_store.connection(self.faces) as db:
            self.assertEqual(dict(db.execute('SELECT state,count(*) FROM face_jobs GROUP BY state').fetchall()), {'queued': 3})
            self.assertEqual(db.execute('SELECT attempts FROM face_jobs WHERE asset=?', (extractor.calls[0],)).fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
