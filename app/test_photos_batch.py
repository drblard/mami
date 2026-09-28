import hashlib
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from import_media import Importer
from photos_batch import PhotosBatch


class PhotosBatchTests(unittest.TestCase):
    @contextlib.contextmanager
    def db(self):
        with contextlib.closing(sqlite3.connect(self.catalog)) as db:
            with db:
                yield db

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.incoming = self.root / 'incoming'
        self.receipts = self.incoming / '.receipts'
        self.receipts.mkdir(parents=True)
        self.source = self.incoming / 'resource' / 'a.mov'
        self.source.parent.mkdir()
        self.source.write_bytes(b'full cloud original')
        self.receipt = self.receipts / 'resource.json'
        self.receipt.write_text(json.dumps(dict(file='resource/a.mov', size=self.source.stat().st_size,
                                              digest=hashlib.sha256(self.source.read_bytes()).hexdigest(), imported=False)))
        self.alias = self.receipts / 'old-export.partial'
        os.link(self.source, self.alias)
        self.catalog = self.root / 'catalog.sqlite'
        with self.db() as db:
            db.executescript('CREATE TABLE photos_import_history(resource TEXT PRIMARY KEY,digest TEXT,size TEXT); '
                             'CREATE TABLE media(path TEXT PRIMARY KEY,asset TEXT,payload TEXT); '
                             'CREATE TABLE state(id INTEGER PRIMARY KEY,identity TEXT,revision INTEGER,change_token TEXT); '
                             'INSERT INTO state VALUES(1,"fixture",0,"initial");')
        self.importer = Importer(self.incoming, self.root / 'originals', 'iCloud', catalog=self.catalog,
                                 direct_destination=True, date_reader=lambda _: ('2026-09-26', 'fixture'))
        self.batch = PhotosBatch(self.importer)
        self.target = self.root / 'originals/2026/2026-09-26/a.mov'

    def test_published_receipt_is_processed_while_next_download_is_unfinished(self):
        unfinished = self.receipts / 'in-progress.partial'
        unfinished.write_bytes(b'still downloading')
        self.batch.complete(self.receipt)
        self.assertEqual(self.target.read_bytes(), b'full cloud original')
        self.assertFalse(self.source.exists())
        self.assertFalse(self.alias.exists())
        self.assertEqual(unfinished.read_bytes(), b'still downloading')
        self.assertTrue(json.loads(self.receipt.read_text())['imported'])
        self.assertFalse(list(self.importer.directory.glob('*.partial')))
        with self.db() as db:
            self.assertEqual(db.execute('SELECT resource FROM photos_import_history').fetchall(), [('resource',)])

    def test_staging_corruption_is_retained(self):
        self.source.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'receipt verification'):
            self.batch.complete(self.receipt)
        self.assertTrue(self.source.exists())
        self.assertFalse(self.target.exists())

    def test_destination_corruption_does_not_complete_or_remove_staging(self):
        original = self.importer.copy_one
        def corrupt(source):
            result = original(source)
            self.target.write_bytes(b'corrupt')
            return result
        with patch.object(self.importer, 'copy_one', corrupt):
            with self.assertRaisesRegex(ValueError, 'Destination changed'):
                self.batch.complete(self.receipt)
        self.assertTrue(self.source.exists())
        with self.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM photos_import_history').fetchone()[0], 0)

    def test_interruption_after_history_commit_retries_cleanup_without_copying(self):
        with patch.object(self.importer, 'remove_verified', side_effect=RuntimeError('interrupted')):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                self.batch.complete(self.receipt)
        self.assertTrue(self.source.exists())
        with patch.object(self.importer, 'copy_one', side_effect=AssertionError('must not recopy')):
            self.batch.complete(self.receipt)
        self.assertFalse(self.source.exists())
        self.assertTrue(self.target.exists())

    def test_offline_archived_completed_original_is_not_recopied(self):
        self.batch.complete(self.receipt)
        self.target.rename(self.root / 'archive.mov')
        with patch.object(self.importer, 'copy_one', side_effect=AssertionError('must not recopy')):
            self.batch.complete(self.receipt)
        self.assertFalse(self.target.exists())

    def test_duplicate_elsewhere_still_creates_chosen_destination(self):
        outside = self.root / 'another-device.mov'
        outside.write_bytes(self.source.read_bytes())
        with self.db() as db:
            db.execute('INSERT INTO media VALUES(?,?,?)', (str(outside), 'sha256:' + hashlib.sha256(outside.read_bytes()).hexdigest(), '{}'))
        self.batch.complete(self.receipt)
        self.assertTrue(self.target.exists())
        self.assertTrue(outside.exists())


if __name__ == '__main__':
    unittest.main()
