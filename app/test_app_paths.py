import os
from pathlib import Path
import tempfile
import sqlite3
import contextlib
import json
import unittest
from unittest.mock import patch

from app_paths import cache_directory,models_directory,personal_database,media_tool
from user_store import connection


class AppPathsTests(unittest.TestCase):
    def test_personal_data_is_separate_and_test_catalogs_stay_isolated(self):
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'MAMI_SUPPORT_ROOT':directory},clear=True):
            root=Path(directory)
            self.assertEqual(personal_database(root/'Derived/Catalog/catalog.sqlite'),root/'Personal/user.sqlite')
            self.assertEqual(personal_database(root/'fixture/catalog.sqlite'),root/'fixture/user.sqlite')
            self.assertEqual(models_directory(),root/'Models')
            with patch.dict(os.environ,{'MAMI_CATALOG':str(root/'fixture')}):
                self.assertEqual(cache_directory(),root/'fixture/runtime-cache')

    def test_helpers_are_bundled_unless_explicitly_overridden(self):
        with patch.dict(os.environ,{},clear=True):
            self.assertEqual(Path(media_tool('ffprobe')).parent.name,'Helpers')
            self.assertNotIn('homebrew',media_tool('ffmpeg'))
        with patch.dict(os.environ,{'MAMI_FFPROBE':'/fixture/ffprobe'}):
            self.assertEqual(media_tool('ffprobe'),'/fixture/ffprobe')
        with self.assertRaises(ValueError):media_tool('../unowned')

    def test_worker_reads_separate_personal_store_and_refuses_missing_owned_data(self):
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'MAMI_SUPPORT_ROOT':directory},clear=True):
            root=Path(directory);catalog=root/'Derived/Catalog/catalog.sqlite';catalog.parent.mkdir(parents=True)
            personal=root/'Personal/user.sqlite';personal.parent.mkdir()
            with contextlib.closing(sqlite3.connect(catalog)) as db,db:
                db.executescript('CREATE TABLE state(id INTEGER,identity TEXT); INSERT INTO state VALUES(1,"library");')
            with contextlib.closing(sqlite3.connect(personal)) as db,db:
                db.executescript('CREATE TABLE user_store_info(version INTEGER,source_identity TEXT); INSERT INTO user_store_info VALUES(1,"library"); CREATE TABLE notes(value TEXT); INSERT INTO notes VALUES("personal");')
            (personal.parent/'ownership.json').write_text(json.dumps(dict(version=1,identity='library')))
            with connection(catalog) as db:self.assertEqual(db.execute('SELECT value FROM notes').fetchone()[0],'personal')
            personal.unlink()
            with self.assertRaisesRegex(ValueError,'Personal database is missing'):
                with connection(catalog):pass


if __name__=='__main__':unittest.main()
