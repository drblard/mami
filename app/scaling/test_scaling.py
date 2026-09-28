import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from common import save, lock
from corpus import device_of
from text_bench import expression
from archive import entries, main as archive_main


class RecoveryTests(unittest.TestCase):
    def test_failed_publication_preserves_last_complete_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'state.json'
            save(path,dict(done=10))
            with patch('common.os.replace',side_effect=OSError('interrupted')):
                with self.assertRaises(OSError):save(path,dict(done=20))
            self.assertEqual(json.loads(path.read_text()),dict(done=10))
            save(path,dict(done=20))
            self.assertEqual(json.loads(path.read_text()),dict(done=20))

    def test_second_job_cannot_take_the_same_run_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            with lock(directory,'build'):
                with self.assertRaises(BlockingIOError):
                    with lock(directory,'build'):pass
            with lock(directory,'build'):pass

    def test_metadata_does_not_invent_resolution_as_a_camera(self):
        self.assertEqual(device_of(dict(url='file:///Users/ludi/Media/Originals/iCloud/one.jpg',
                                        metadata=dict(source='iCloud',camera='',details=['12 MP']))),
                         'iCloud · Device unavailable')
        self.assertEqual(device_of(dict(url='file:///Users/ludi/Media/Originals/DJI-Pocket-4P/one.mp4',
                                        metadata=dict(camera='DJI PP-041'))),'DJI-Pocket-4P')
        self.assertEqual(device_of(dict(url='file:///Users/ludi/Media/Originals/iCloud/one.mov',
                                        metadata=dict(source='iCloud',camera='Apple iPhone 16 Pro Max'))),
                         'Apple iPhone 16 Pro Max')

    def test_fts_expression_quotes_operators_and_folds_accents(self):
        self.assertEqual(expression('Dunăre cap'), '"dunare" AND "cap"*')
        self.assertEqual(expression('" OR *'), '"or"*')
        self.assertEqual(expression('???'), '')

    def test_archive_refuses_source_changed_after_destination_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'source/benchmarks/scaling-test'
            destination=Path(directory)/'destination/benchmarks/scaling-test'
            root.mkdir(parents=True);destination.mkdir(parents=True)
            (root/'graph-50x.usearch').write_bytes(b'checkpoint')
            (destination/'graph-50x.usearch').write_bytes(b'checkpoint')
            manifest=root/'manifest.json';receipt=destination/'receipt.json'
            args=SimpleNamespace(action='manifest',root=root,artifact='graph-50x.usearch',manifest=manifest,receipt=None)
            archive_main(args)
            archive_main(SimpleNamespace(**{**vars(args),'action':'verify','root':destination,'receipt':receipt}))
            (root/'graph-50x.usearch').write_bytes(b'new-checkpoint')
            with self.assertRaises(ValueError):
                archive_main(SimpleNamespace(**{**vars(args),'action':'remove','receipt':receipt}))
            self.assertEqual((root/'graph-50x.usearch').read_bytes(),b'new-checkpoint')

    def test_archive_scope_and_symlink_guards(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with self.assertRaises(ValueError):entries(root,'../../originals')
            (root/'outside').mkdir()
            (root/'lance-50x').symlink_to(root/'outside',target_is_directory=True)
            with self.assertRaises(ValueError):entries(root,'lance-50x')


if __name__=='__main__':unittest.main()
