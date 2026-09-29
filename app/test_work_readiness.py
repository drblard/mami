import contextlib
from pathlib import Path
import sqlite3
import tempfile
import unittest

from index_store import connection,ensure_schema
from prepare_storage import prepare


class WorkReadinessTests(unittest.TestCase):
    def test_persistent_readiness_tracks_preview_priority_pause_and_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'catalog.sqlite'
            with contextlib.closing(sqlite3.connect(path)) as db,db:
                db.executescript('CREATE TABLE state(id INTEGER,identity TEXT,revision INTEGER,change_token TEXT); INSERT INTO state VALUES(1,"fixture",0,"token"); CREATE TABLE media(path TEXT,asset TEXT,payload TEXT);')
            with connection(path) as db:
                ensure_schema(db)
                for asset in ('first','second'):
                    db.execute("INSERT INTO index_jobs(asset,path,kind,signature,logical,state) VALUES(?,?,?,?,?,'queued')",(asset,asset,'video','signature',asset))
                db.execute("UPDATE preview_jobs SET state='complete' WHERE asset='first'")
                self.assertEqual(db.execute('SELECT asset FROM mami_preview_work').fetchall()[0][0],'second')
                self.assertEqual(db.execute('SELECT * FROM mami_index_work').fetchall(),[])
                db.execute('UPDATE preview_control SET paused=1')
                self.assertEqual(db.execute('SELECT asset FROM mami_index_work').fetchall()[0][0],'first')
                db.execute('UPDATE scan_control SET paused=1')
                self.assertEqual(db.execute('SELECT * FROM mami_index_work').fetchall(),[])
                db.execute('UPDATE scan_control SET paused=0')
                db.execute("UPDATE index_jobs SET state='complete' WHERE asset='first'")
                self.assertEqual(db.execute('SELECT * FROM mami_index_work').fetchall(),[])
            projection=Path(directory)/'search.sqlite'
            prepare(path,projection)
            prepare(path,projection)
            with contextlib.closing(sqlite3.connect(projection)) as db:
                self.assertEqual(db.execute('SELECT ready FROM checkpoint').fetchone()[0],1)
                self.assertEqual(db.execute('SELECT count(*) FROM files').fetchone()[0],0)


if __name__=='__main__':unittest.main()
