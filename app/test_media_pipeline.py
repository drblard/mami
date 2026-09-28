import contextlib
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from import_media import Importer
from index_queue import Queue
from index_store import register_verified_import
from test_index_queue import Backend


class MediaPipelineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.card = self.root/'card'
        self.card.mkdir()
        self.originals = self.root/'originals'
        self.database = self.root/'catalog.sqlite'
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            db.executescript('CREATE TABLE state(id INTEGER PRIMARY KEY,identity TEXT,revision INTEGER,change_token TEXT);'
                             'INSERT INTO state VALUES(1,"fixture",0,"initial");'
                             'CREATE TABLE media(path TEXT PRIMARY KEY,asset TEXT,payload TEXT);')
        self.backend = Backend()

    def read(self, sql):
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            return db.execute(sql).fetchall()

    def import_four(self, emit=lambda value: None):
        for index in range(4):
            (self.card/f'DJI_20260928120{index}00_0001_D.MP4').write_bytes(f'original {index}'.encode())
        importer = Importer(self.card, self.originals, 'Camera', catalog=self.database,
                            date_reader=lambda _: ('2026-09-28', 'fixture'), emit=emit)
        importer.run()
        self.assertEqual((importer.copied + importer.duplicates, importer.failed), (4, 0))
        return importer

    def worker(self, role, emit=lambda value: None):
        return Queue(self.database, self.originals, self.root/'artifacts', self.backend, emit, role=role)

    def test_each_verified_file_is_visible_before_any_preview_or_ai_work(self):
        publications = []
        self.import_four(lambda event: publications.append(len(self.read('SELECT path FROM media'))) if event['catalog_changed'] else None)
        self.assertEqual(publications, [1, 2, 3, 4])
        for path, payload in self.read('SELECT path,payload FROM media'):
            media = json.loads(payload)
            self.assertTrue(Path(path).is_file())
            self.assertEqual(media['frames'], [])
            self.assertEqual(media['previewState'], 'pending')
        self.assertEqual(self.backend.frames, [])
        self.assertEqual(self.backend.vectors, [])

    def test_all_thumbnails_precede_scrubbing_and_preview_worker_never_runs_ai(self):
        self.import_four()
        publications = []
        def emitted(event):
            if event['changed']:
                publications.append([len(json.loads(row[0])['frames']) for row in self.read('SELECT payload FROM media')])
        preview = self.worker('preview', emitted)
        with patch.object(self.backend, 'embedding', side_effect=AssertionError('Preview must not load AI')), \
             patch.object(self.backend, 'speech', side_effect=AssertionError('Preview must not transcribe')):
            preview.work()
        first_scrub = next(counts for counts in publications if max(counts) > 1)
        self.assertTrue(all(count >= 1 for count in first_scrub))
        self.assertEqual(self.read("SELECT count(*) FROM preview_jobs WHERE state='complete'")[0][0], 4)
        self.assertEqual(self.read("SELECT count(*) FROM index_jobs WHERE state='queued'")[0][0], 4)
        indexer = self.worker('index')
        with patch.object(self.backend, 'frame', side_effect=AssertionError('AI must reuse previews')):
            indexer.work()
        self.assertEqual(len(self.backend.vectors), 12)
        self.assertEqual(self.read("SELECT count(*) FROM index_jobs WHERE state='complete'")[0][0], 4)

    def test_ai_pause_does_not_pause_previews_and_recovery_is_role_scoped(self):
        self.import_four()
        indexer = self.worker('index')
        indexer.command(dict(action='pause'))
        with indexer.db() as db:
            asset = db.execute('SELECT asset FROM index_jobs LIMIT 1').fetchone()[0]
            db.execute("UPDATE index_jobs SET state='running' WHERE asset=?", (asset,))
        preview = self.worker('preview')
        self.assertFalse(preview.paused.is_set())
        self.assertEqual(self.read("SELECT count(*) FROM index_jobs WHERE state='running'")[0][0], 1)
        preview.work()
        self.assertEqual(self.read("SELECT count(*) FROM preview_jobs WHERE state='complete'")[0][0], 4)
        self.assertEqual(self.backend.vectors, [])

    def test_reimport_does_not_replace_ready_frames_with_placeholders(self):
        self.import_four()
        self.worker('preview').work()
        before = self.read('SELECT path,payload FROM media ORDER BY path')
        self.import_four()
        self.assertEqual(before, self.read('SELECT path,payload FROM media ORDER BY path'))

    def test_verified_duplicate_does_not_create_duplicate_logical_grid_ids(self):
        self.originals.mkdir()
        first = self.originals/'first.mp4'
        second = self.originals/'second.mp4'
        first.write_bytes(b'same original')
        second.write_bytes(first.read_bytes())
        digest = hashlib.sha256(first.read_bytes()).hexdigest()
        self.assertTrue(register_verified_import(self.database, first, digest, capture_time=1, source_device='Camera'))
        self.assertFalse(register_verified_import(self.database, second, digest, capture_time=1, source_device='Camera'))
        self.assertEqual(len(self.read('SELECT path FROM media')), 1)


if __name__ == '__main__':
    unittest.main()
