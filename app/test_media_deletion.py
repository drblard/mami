import contextlib
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from unittest.mock import patch

from import_media import Importer
from index_queue import Queue
import media_deletion
from media_deletion import deleted, forget, release_staging
from photos_batch import PhotosBatch
from test_index_queue import Backend

DELETED_AT = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


class MediaDeletionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'originals'
        self.root.mkdir()
        self.take = self.root / 'take1.mp4'
        self.take.write_bytes(b'first take')
        self.kept = self.root / 'take2.mp4'
        self.kept.write_bytes(b'second take')
        self.database = self.base / 'catalog.sqlite'
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            db.executescript('CREATE TABLE state(id INTEGER PRIMARY KEY, identity TEXT, revision INTEGER, change_token TEXT); INSERT INTO state VALUES(1,"test",0,"initial"); CREATE TABLE media(path TEXT PRIMARY KEY, asset TEXT, payload TEXT);')
        self.queue = Queue(self.database, self.root, self.base / 'artifacts', Backend())
        self.queue.scan()
        self.queue.work()
        self.asset = 'sha256:' + hashlib.sha256(b'first take').hexdigest()
        self.kept_asset = 'sha256:' + hashlib.sha256(b'second take').hexdigest()

    def rows(self, sql, *parameters):
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            return db.execute(sql, parameters).fetchall()

    def assets(self, table):
        return sorted(row[0] for row in self.rows(f'SELECT DISTINCT asset FROM {table}'))

    def test_forget_removes_generated_state_and_records_deletion(self):
        both = sorted([self.asset, self.kept_asset])
        for table in ('media', 'scan_files', 'index_units', 'preview_jobs', 'index_jobs'):
            self.assertEqual(self.assets(table), both, table)
        revision = self.rows('SELECT revision FROM state')[0][0]
        result = forget(self.database, [self.asset], now=DELETED_AT)
        for table in ('media', 'scan_files', 'index_units', 'preview_jobs', 'index_jobs'):
            self.assertEqual(self.assets(table), [self.kept_asset], table)
        self.assertEqual(result['removed']['media'], 1)
        self.assertEqual(result['removed']['index_jobs'], 1)
        self.assertEqual(self.rows('SELECT * FROM deleted_media'),
                         [(self.asset, json.dumps([str(self.take)]), DELETED_AT.isoformat())])
        self.assertGreater(self.rows('SELECT revision FROM state')[0][0], revision)
        self.assertTrue(deleted(self.database, self.asset.split(':')[1]))
        self.assertFalse(deleted(self.database, self.kept_asset.split(':')[1]))

    def test_in_flight_publish_after_deletion_does_not_restore_media(self):
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            db.row_factory = sqlite3.Row
            job = dict(db.execute('SELECT * FROM index_jobs WHERE asset=?', (self.asset,)).fetchone())
            frames = json.loads(db.execute('SELECT payload FROM media WHERE asset=?', (self.asset,)).fetchone()[0])['frames']
        forget(self.database, [self.asset], now=DELETED_AT)
        self.queue.publish(job, None, frames)
        self.assertEqual(self.assets('media'), [self.kept_asset])

    def test_put_back_from_trash_is_indexed_again(self):
        trash = self.base / 'Trash'
        trash.mkdir()
        self.take.rename(trash / self.take.name)
        forget(self.database, [self.asset], {str(self.take): str(trash / self.take.name)}, now=DELETED_AT)
        self.queue.scan()
        self.assertEqual(self.assets('index_jobs'), [self.kept_asset])
        (trash / self.take.name).rename(self.take)
        self.queue.scan()
        self.queue.work()
        self.assertEqual(self.assets('media'), sorted([self.asset, self.kept_asset]))

    def test_camera_import_skips_deleted_content_and_keeps_card_copy(self):
        forget(self.database, [self.asset], now=DELETED_AT)
        card = self.base / 'card'
        card.mkdir()
        (card / 'take1.mp4').write_bytes(b'first take')
        (card / 'take3.mp4').write_bytes(b'third take')
        destination = self.base / 'imports'
        importer = Importer(card, destination, 'Camera', catalog=self.database, remove_source=True,
                            date_reader=lambda _: ('2026-09-18', 'fixture'))
        importer.run()
        self.assertEqual((importer.copied, importer.skipped, importer.failed), (1, 1, 0))
        self.assertEqual(sorted(path.name for path in (destination / 'Camera').rglob('*.mp4')), ['take3.mp4'])
        self.assertTrue((card / 'take1.mp4').exists())
        self.assertFalse((card / 'take3.mp4').exists())

    def card_import(self, contents, **options):
        card = self.base / 'card'
        card.mkdir(exist_ok=True)
        (card / 'take1.mp4').write_bytes(contents)
        importer = Importer(card, self.root, 'Camera', catalog=self.database,
                            date_reader=lambda _: ('2026-09-18', 'fixture'), **options)
        importer.run()
        return card, importer

    def test_put_back_unblocks_camera_import_and_card_cleanup(self):
        trash = self.base / 'Trash'
        trash.mkdir()
        self.take.rename(trash / self.take.name)
        forget(self.database, [self.asset], {str(self.take): str(trash / self.take.name)}, now=DELETED_AT)
        (trash / self.take.name).rename(self.take)
        self.queue.scan()
        self.assertFalse(deleted(self.database, self.asset.split(':')[1]))
        self.queue.work()
        card, importer = self.card_import(b'first take', remove_source=True)
        self.assertEqual((importer.copied, importer.duplicates, importer.skipped, importer.failed), (0, 1, 0, 0))
        self.assertFalse((card / 'take1.mp4').exists())

    def test_deletion_that_fails_in_the_catalog_does_not_block_imports(self):
        with patch.object(media_deletion, 'catalog_connection', side_effect=sqlite3.OperationalError('database is locked')):
            with self.assertRaises(sqlite3.OperationalError):
                forget(self.database, [self.asset], now=DELETED_AT)
        self.assertEqual(len(self.rows('SELECT * FROM deleted_media')), 1)
        self.assertEqual(self.assets('media'), sorted([self.asset, self.kept_asset]))
        self.assertFalse(deleted(self.database, self.asset.split(':')[1]))

    def test_photos_receipt_for_deleted_content_is_skipped_and_released(self):
        with contextlib.closing(sqlite3.connect(self.database)) as db, db:
            db.execute('CREATE TABLE photos_import_history(resource TEXT PRIMARY KEY,digest TEXT,size TEXT)')
        forget(self.database, [self.asset], now=DELETED_AT)
        incoming = self.base / 'incoming'
        (incoming / '.receipts').mkdir(parents=True)
        staged = incoming / 'resource' / 'take1.mp4'
        staged.parent.mkdir()
        staged.write_bytes(b'first take')
        receipt = incoming / '.receipts' / 'resource.json'
        receipt.write_text(json.dumps(dict(file='resource/take1.mp4', size=staged.stat().st_size,
                                           digest=self.asset.split(':')[1], imported=False)))
        importer = Importer(incoming, self.base / 'icloud', 'iCloud', catalog=self.database, direct_destination=True,
                            date_reader=lambda _: ('2026-09-18', 'fixture'))
        PhotosBatch(importer).complete(receipt)
        self.assertEqual(importer.skipped, 1)
        self.assertFalse(staged.exists())
        self.assertEqual(list((self.base / 'icloud').rglob('*.mp4')), [])
        self.assertEqual(self.rows('SELECT resource FROM photos_import_history'), [('resource',)])
        saved = json.loads(receipt.read_text())
        self.assertEqual((saved['imported'], saved['deleted']), (True, True))

    def test_release_staging_unlinks_only_the_trashed_file(self):
        imports = self.root / '.mami-imports'
        imports.mkdir()
        staged = imports / 'staged.partial'
        os.link(self.take, staged)
        copy = imports / 'copy.partial'
        copy.write_bytes(b'first take')
        with contextlib.closing(sqlite3.connect(imports / 'journal.sqlite')) as db, db:
            db.execute('CREATE TABLE files(source TEXT, signature TEXT, device TEXT, digest TEXT, date TEXT, date_source TEXT, part TEXT, destination TEXT)')
            db.executemany('INSERT INTO files VALUES(?,?,?,?,?,?,?,?)', [
                ('card/take1', 'one', 'Camera', 'digest', '2026-09-18', 'fixture', str(staged), str(self.take)),
                ('card/take1-again', 'two', 'Camera', 'digest', '2026-09-18', 'fixture', str(copy), str(self.take)),
            ])
        trashed = self.base / 'take1 in Trash.mp4'
        self.take.rename(trashed)
        self.assertEqual(release_staging({str(self.take): str(trashed)}), (1, []))
        self.assertFalse(staged.exists())
        self.assertEqual(copy.read_bytes(), b'first take')
        self.assertEqual(trashed.read_bytes(), b'first take')

    def test_invalid_identity_changes_nothing(self):
        with self.assertRaises(ValueError):
            forget(self.database, [self.asset, '../media'], now=DELETED_AT)
        self.assertEqual(self.assets('media'), sorted([self.asset, self.kept_asset]))
        self.assertEqual(self.rows("SELECT name FROM sqlite_master WHERE name='deleted_media'"), [])


if __name__ == '__main__':
    unittest.main()
