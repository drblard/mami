import contextlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from user_store import connection
from restore_catalog import restore


class UserStoreTests(unittest.TestCase):
    def create(self, root, migrated=True):
        catalog=root/'catalog.sqlite'
        with contextlib.closing(sqlite3.connect(catalog)) as db, db:
            db.executescript("CREATE TABLE state(id INTEGER PRIMARY KEY,identity TEXT); INSERT INTO state VALUES(1,'library');"
                             "CREATE TABLE photos_import_history(resource TEXT PRIMARY KEY,digest TEXT,size TEXT);")
        if migrated:
            with contextlib.closing(sqlite3.connect(root/'user.sqlite')) as db, db:
                db.executescript("CREATE TABLE user_store_info(version INTEGER,source_identity TEXT); INSERT INTO user_store_info VALUES(1,'library');"
                                 "CREATE TABLE photos_import_history(resource TEXT PRIMARY KEY,digest TEXT,size TEXT);")
        return catalog

    def test_import_ledger_writes_only_personal_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);catalog=self.create(root)
            with connection(catalog,write=True) as db:
                db.execute('INSERT INTO photos_import_history VALUES(?,?,?)',('photo','hash','123'))
            with connection(catalog) as db:
                self.assertEqual(db.execute('SELECT resource FROM photos_import_history').fetchall(),[('photo',)])
            with contextlib.closing(sqlite3.connect(catalog)) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM photos_import_history').fetchone()[0],0)

    def test_unmigrated_fixture_and_transaction_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog=self.create(Path(directory),migrated=False)
            with self.assertRaises(RuntimeError):
                with connection(catalog,write=True) as db:
                    db.execute("INSERT INTO photos_import_history VALUES('photo','hash','123')")
                    raise RuntimeError('interrupted')
            with connection(catalog) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM photos_import_history').fetchone()[0],0)

    def test_mismatched_user_store_is_not_silently_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);catalog=self.create(root)
            with contextlib.closing(sqlite3.connect(root/'user.sqlite')) as db, db:
                db.execute("UPDATE user_store_info SET source_identity='other'")
            with self.assertRaises(ValueError):
                with connection(catalog):pass

    def test_missing_migrated_store_does_not_use_stale_legacy_history(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog=self.create(Path(directory),migrated=False)
            with contextlib.closing(sqlite3.connect(catalog)) as db, db:
                db.execute('CREATE TABLE user_store_migration(id INTEGER PRIMARY KEY)')
            with self.assertRaises(ValueError):
                with connection(catalog,write=True):pass

    def test_personal_snapshot_restore_creates_only_user_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'personal.sqlite'
            with contextlib.closing(sqlite3.connect(source)) as db, db:
                db.executescript("PRAGMA user_version=1; CREATE TABLE user_store_info(version INTEGER,source_identity TEXT);"
                                 "INSERT INTO user_store_info VALUES(1,'library');"
                                 "CREATE TABLE state(id INTEGER PRIMARY KEY,identity TEXT,revision INTEGER,change_token TEXT);"
                                 "INSERT INTO state VALUES(1,'library',2,'token');"
                                 "CREATE TABLE annotations(asset TEXT PRIMARY KEY,payload TEXT);"
                                 "INSERT INTO annotations VALUES('sha256:media','favorite');"
                                 "CREATE TABLE annotation_history(id INTEGER PRIMARY KEY,asset TEXT,payload TEXT);")
            result=restore(source,root/'restored')
            self.assertEqual(result['kind'],'user-state')
            self.assertTrue((root/'restored/user.sqlite').exists())
            self.assertFalse((root/'restored/catalog.sqlite').exists())
            with contextlib.closing(sqlite3.connect(root/'restored/user.sqlite')) as db:
                self.assertEqual(db.execute('SELECT payload FROM annotations').fetchone()[0],'favorite')


if __name__=='__main__':unittest.main()
