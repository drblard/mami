import fcntl
import json
import os
from pathlib import Path
import tempfile
import unittest

from vector_sync import prune,publish,resolve_generation
from packed_vectors import FORMAT_VERSION


class VectorPublicationTests(unittest.TestCase):
    def test_cleanup_keeps_readers_current_recent_and_unowned_generations(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'owner.json').write_text(json.dumps(dict(id='owner')))
            generations=[]
            for i in range(6):
                path=root/f'generation-{i}';path.mkdir()
                (path/'manifest.json').write_text(json.dumps(dict(version=FORMAT_VERSION,owner='owner')))
                (path/'.readers').touch()
                os.utime(path,(i+1,i+1))
                generations.append(path)
            foreign=root/'generation-foreign';foreign.mkdir()
            (foreign/'manifest.json').write_text(json.dumps(dict(version=FORMAT_VERSION,owner='other')))
            publish(root,generations[5])
            with (generations[0]/'.readers').open('rb') as reader:
                fcntl.flock(reader,fcntl.LOCK_SH)
                prune(root)
                self.assertTrue(generations[0].exists())
                self.assertFalse(generations[1].exists())
                self.assertFalse(generations[2].exists())
                self.assertTrue(all(path.exists() for path in generations[3:]))
                self.assertTrue(foreign.exists())
            prune(root)
            self.assertFalse(generations[0].exists())
            self.assertEqual(resolve_generation(root),generations[5])

    def test_pointer_cannot_escape_managed_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'current.json').write_text(json.dumps(dict(generation='../outside')))
            with self.assertRaises(ValueError):resolve_generation(root)


if __name__=='__main__':unittest.main()
