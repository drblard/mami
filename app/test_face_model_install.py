import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import download_models


def digest(data):
    return hashlib.sha256(data).hexdigest()


class FaceModelInstallTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.archive = self.root / 'model.zip'
        with zipfile.ZipFile(self.archive, 'w') as bundle:
            bundle.writestr('pack/detector.onnx', b'detector')
            bundle.writestr('pack/recognizer.onnx', b'recognizer')
            bundle.writestr('pack/unused.onnx', b'unused')
        self.model = dict(name='pack', archive_sha256=digest(self.archive.read_bytes()),
                          detector=('detector.onnx', digest(b'detector')), recognizer=('recognizer.onnx', digest(b'recognizer')))
        self.target = self.root / 'Models' / 'faces' / 'pack'

    def tearDown(self):
        self.directory.cleanup()

    def install(self, model):
        with mock.patch.object(download_models, 'FACE_MODEL', model):
            return download_models.install_face_model(self.archive, self.target)

    def test_verified_archive_publishes_only_the_pinned_files(self):
        self.assertEqual(self.install(self.model), self.target)
        self.assertEqual(sorted(path.name for path in self.target.iterdir()), ['detector.onnx', 'recognizer.onnx'])
        self.assertEqual(sorted(path.name for path in self.target.parent.iterdir()), ['pack'])
        self.assertEqual(self.install(self.model), self.target)

    def test_wrong_archive_or_file_digest_publishes_nothing(self):
        for model in (dict(self.model, archive_sha256='0' * 64), dict(self.model, recognizer=('recognizer.onnx', '0' * 64))):
            with self.assertRaises(ValueError):
                self.install(model)
            self.assertFalse(self.target.exists())
            self.assertEqual(list(self.target.parent.iterdir()), [])

    def test_tampered_installed_file_is_detected(self):
        self.install(self.model)
        (self.target / 'detector.onnx').write_bytes(b'changed')
        with self.assertRaises(ValueError):
            self.install(self.model)


if __name__ == '__main__':
    unittest.main()
