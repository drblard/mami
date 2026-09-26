import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import threading

from import_media import Importer


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / 'card'
        self.source.mkdir()
        self.destination = self.base / 'originals'

    def importer(self, **kwargs):
        return Importer(self.source, self.destination, 'Camera', date_reader=lambda _: ('2026-09-18', 'fixture'), **kwargs)

    def test_verified_copy_duplicate_proxy_exclusion_and_collision(self):
        (self.source / 'a.mp4').write_bytes(b'original footage')
        (self.source / 'duplicate.MOV').write_bytes(b'original footage')
        (self.source / 'proxy.LRF').write_bytes(b'camera proxy')
        folder = self.destination / 'Camera/2026/2026-09-18'
        folder.mkdir(parents=True)
        (folder / 'a.mp4').write_bytes(b'previous unrelated original')
        importer = self.importer()
        importer.run()
        self.assertEqual((importer.copied, importer.duplicates, importer.skipped, importer.failed), (1, 1, 1, 0))
        self.assertEqual((folder / 'a.mp4').read_bytes(), b'previous unrelated original')
        self.assertEqual(len(list(folder.iterdir())), 2)
        self.assertEqual((self.source / 'a.mp4').read_bytes(), b'original footage')
        retry = self.importer()
        retry.run()
        self.assertEqual((retry.copied, retry.duplicates), (0, 2))

    def crash_after_first_chunk(self):
        data = b'original!' * 1024 * 1024
        (self.source / 'a.mp4').write_bytes(data)
        script = """
import os, sys
from import_media import Importer
def emit(value):
    if value['phase'] == 'Copying' and value['bytes_done'] > 0:
        os._exit(77)
Importer(sys.argv[1], sys.argv[2], 'Camera', emit=emit, date_reader=lambda _: ('2026-09-18','fixture')).run()
"""
        result = subprocess.run([sys.executable, '-c', script, str(self.source), str(self.destination)], cwd=Path(__file__).parent)
        self.assertEqual(result.returncode, 77)
        return data, next((self.destination / '.mami-imports').glob('*.partial'))

    def test_process_crash_resumes_same_partial_then_verifies(self):
        data, part = self.crash_after_first_chunk()
        inode = part.stat().st_ino
        self.assertLess(part.stat().st_size, len(data))
        self.assertFalse(list(self.destination.glob('Camera/**/*mp4')))
        importer = self.importer()
        importer.run()
        self.assertEqual(importer.failed, 0)
        self.assertEqual(part.stat().st_ino, inode)
        target = self.destination / 'Camera/2026/2026-09-18/a.mp4'
        self.assertEqual(hashlib.sha256(target.read_bytes()).digest(), hashlib.sha256(data).digest())

    def test_damaged_partial_is_retained_and_replaced(self):
        data, part = self.crash_after_first_chunk()
        part.write_bytes(b'damaged checkpoint')
        importer = self.importer()
        importer.run()
        self.assertEqual(part.read_bytes(), b'damaged checkpoint')
        self.assertEqual((self.destination / 'Camera/2026/2026-09-18/a.mp4').read_bytes(), data)
        self.assertEqual(importer.failed, 0)

    def test_source_mutation_never_publishes(self):
        source = self.source / 'a.mp4'
        source.write_bytes(b'original')
        def event(value):
            if value['phase'] == 'Copying': source.write_bytes(b'changed')
        importer = self.importer(emit=event)
        importer.run()
        self.assertEqual(importer.failed, 1)
        self.assertFalse(list(self.destination.glob('Camera/**/*mp4')))

    def test_rejects_recursive_source_and_bad_device(self):
        for device in ('../escape', '.', '  ', '.hidden', ' .. ', ' .hidden '):
            with self.assertRaises(ValueError):
                Importer(self.source, self.destination, device)
        with self.assertRaises(ValueError):
            Importer(self.base, self.destination, 'Camera')

    def test_corrupt_existing_copy_is_not_trusted_as_duplicate(self):
        source = self.source / 'a.mp4'
        source.write_bytes(b'original')
        self.importer().run()
        target = self.destination / 'Camera/2026/2026-09-18/a.mp4'
        target.write_bytes(b'damaged')
        importer = self.importer()
        importer.run()
        self.assertEqual((importer.copied, importer.duplicates, importer.failed), (1, 0, 0))
        self.assertEqual(target.read_bytes(), b'damaged')
        self.assertEqual([p.read_bytes() for p in target.parent.glob('a-*.mp4')], [b'original'])

    def test_pause_blocks_copy_until_resume(self):
        (self.source / 'a.mp4').write_bytes(b'original')
        waiting = threading.Event()
        importer = self.importer(emit=lambda value: waiting.set() if value['paused'] else None)
        importer.paused.set()
        thread = threading.Thread(target=importer.run)
        thread.start()
        try:
            self.assertTrue(waiting.wait(2))
            self.assertFalse(list(importer.directory.glob('*.partial')))
            importer.paused.clear()
            importer.wake.set()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(importer.copied, 1)
        finally:
            importer.stop.set()
            importer.wake.set()
            thread.join(3)

    def test_remove_only_verified_media_and_honor_exceptions(self):
        for name in ('remove.mp4', 'keep.mov', 'skip.jpg', 'proxy.LRF'):
            (self.source / name).write_bytes(name.encode())
        importer = self.importer(remove_source=True, policy_json=json.dumps({'keep.mov': 'keep', 'skip.jpg': 'skip'}))
        importer.run()
        self.assertEqual((importer.removed, importer.copied, importer.failed), (1, 2, 0))
        self.assertFalse((self.source / 'remove.mp4').exists())
        self.assertEqual({p.name for p in self.source.iterdir()}, {'keep.mov', 'skip.jpg', 'proxy.LRF'})
        self.assertFalse(list(self.destination.glob('Camera/**/skip.jpg')))
        with importer.db() as db:
            receipt = db.execute('SELECT * FROM removals').fetchone()
            self.assertEqual(receipt['state'], 'removed')
            self.assertEqual(hashlib.sha256(Path(receipt['destination']).read_bytes()).hexdigest(), receipt['digest'])

    def test_duplicate_requires_fresh_destination_read_before_removal(self):
        source = self.source / 'a.mp4'; source.write_bytes(b'original')
        self.importer().run()
        importer = self.importer(remove_source=True)
        importer.run()
        self.assertEqual((importer.duplicates, importer.removed, importer.failed), (1, 1, 0))
        self.assertFalse(source.exists())

    def test_final_corruption_or_source_change_prevents_removal(self):
        source = self.source / 'a.mp4'; source.write_bytes(b'original')
        self.importer().run()
        target = self.destination / 'Camera/2026/2026-09-18/a.mp4'
        for mutate in (target, source):
            source.write_bytes(b'original'); target.write_bytes(b'original')
            fired = []
            def event(value):
                if value['phase'] == 'Rechecking saved original before removal' and not fired:
                    fired.append(True); mutate.write_bytes(b'changed!')
            importer = self.importer(remove_source=True, emit=event)
            importer.run()
            self.assertTrue(fired)
            self.assertEqual((importer.removed, importer.failed), (0, 1))
            self.assertTrue(source.exists())

    def test_crash_after_removal_receipt_rechecks_bytes_on_resume(self):
        source = self.source / 'a.mp4'; source.write_bytes(b'original')
        script = """
import os,sys
from pathlib import Path
from import_media import Importer
original_unlink = Path.unlink
def crash_before_removal(path, *args, **kwargs):
    if path == Path(sys.argv[1]) / 'a.mp4': os._exit(78)
    return original_unlink(path, *args, **kwargs)
Path.unlink = crash_before_removal
Importer(sys.argv[1],sys.argv[2],'Camera',remove_source=True,date_reader=lambda _:('2026-09-18','fixture')).run()
"""
        result = subprocess.run([sys.executable, '-c', script, str(self.source), str(self.destination)], cwd=Path(__file__).parent)
        self.assertEqual(result.returncode, 78)
        self.assertEqual(source.read_bytes(), b'original')
        target = self.destination / 'Camera/2026/2026-09-18/a.mp4'
        target.write_bytes(b'corrupt!')
        importer = self.importer(remove_source=True)
        importer.run()
        self.assertEqual((importer.copied, importer.removed, importer.failed), (1, 1, 0))
        self.assertFalse(source.exists())
        self.assertEqual(target.read_bytes(), b'corrupt!')
        with importer.db() as db:
            receipt = db.execute("SELECT * FROM removals WHERE state='removed'").fetchone()
            self.assertEqual(Path(receipt['destination']).read_bytes(), b'original')


if __name__ == '__main__':
    unittest.main()
