import contextlib
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest

from personal_audit import audit, changed_tables
from user_store import PERSONAL_TABLES


class PersonalAuditTests(unittest.TestCase):
    def test_python_and_swift_personal_tables_match(self):
        source = (Path(__file__).parent / 'Sources/Mami/PersonalDataMigration.swift').read_text()
        declaration = re.search(r'static let tables = \[(.*?)\]', source, re.S).group(1)
        self.assertEqual(tuple(re.findall(r'"(\w+)"', declaration)), PERSONAL_TABLES)

    def test_digest_detects_edits_but_ignores_growing_history(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'user.sqlite'
            with contextlib.closing(sqlite3.connect(database)) as db, db:
                db.executescript("CREATE TABLE annotations(asset TEXT PRIMARY KEY, payload TEXT); INSERT INTO annotations VALUES('a','{}');"
                                 "CREATE TABLE photos_import_history(resource TEXT PRIMARY KEY, digest TEXT, size TEXT);")
            before = audit(database)
            self.assertEqual(before['annotations']['rows'], 1)
            with contextlib.closing(sqlite3.connect(database)) as db, db:
                db.execute("INSERT INTO photos_import_history VALUES('r','d','1')")
            self.assertEqual(changed_tables(before, audit(database)), [])
            with contextlib.closing(sqlite3.connect(database)) as db, db:
                db.execute("UPDATE annotations SET payload='{\"favorite\":true}'")
            self.assertEqual(changed_tables(before, audit(database)), ['annotations'])


if __name__ == '__main__':
    unittest.main()
