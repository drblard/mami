import contextlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

try:
    import numpy as np
except ImportError:
    np=None
from search_store import SearchStore,install_change_log
from model_config import PIPELINE
from vector_overlay import VectorOverlay


@unittest.skipIf(np is None,'NumPy required for overlay checks')
class VectorOverlayTests(unittest.TestCase):
    def test_insert_replace_delete_and_failed_refresh_preserve_atomic_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'catalog.sqlite'
            with contextlib.closing(sqlite3.connect(source)) as db,db:
                db.executescript('CREATE TABLE state(id INTEGER PRIMARY KEY,identity TEXT); INSERT INTO state VALUES(1,"fixture");'
                                 'CREATE TABLE media(path TEXT PRIMARY KEY,asset TEXT,payload TEXT);'
                                 'CREATE TABLE index_units(asset TEXT,pipeline TEXT,stage TEXT,ordinal INTEGER,payload TEXT,PRIMARY KEY(asset,pipeline,stage,ordinal));')
            install_change_log(source)
            with SearchStore(root/'search.sqlite') as store:
                while store.refresh(source):pass
                state=store.db.execute('SELECT * FROM checkpoint').fetchone()
                class Base:
                    manifest=dict(projection_sequence=0,source_identity='fixture',epoch=state['epoch'],sequence=state['sequence'])
                    def search(self,*args,**kwargs):return []
                overlay=VectorOverlay(Base(),store)
                query=np.zeros(768,dtype=np.float32);query[0]=1
                vector=root/'first.npy';np.save(vector,query)
                sample=dict(path='a',kind='video',timestamp=.5,frame='/offline/a.jpg')
                media=dict(path='a',assetID='a',url='file:///offline/a.mp4',kind='video',frames=[sample],match=sample,metadata={})
                with contextlib.closing(sqlite3.connect(source)) as db,db:
                    db.execute('INSERT INTO media VALUES(?,?,?)',('a','a',json.dumps(media)))
                    db.execute('INSERT INTO index_units VALUES(?,?,?,?,?)',('a',PIPELINE,'embedding',0,json.dumps(dict(vector=str(vector),sample=sample))))
                while store.refresh(source):pass
                self.assertTrue(overlay.refresh())
                self.assertAlmostEqual(overlay.search(query)[0]['score'],1)
                previous=overlay.snapshot
                with contextlib.closing(sqlite3.connect(source)) as db,db:
                    db.execute('UPDATE index_units SET payload=?',(json.dumps(dict(vector=str(root/'missing.npy'),sample=sample)),))
                while store.refresh(source):pass
                with self.assertRaises(FileNotFoundError):overlay.refresh()
                self.assertIs(overlay.snapshot,previous)
                replacement=root/'missing.npy';changed=np.zeros(768,dtype=np.float32);changed[1]=1;np.save(replacement,changed)
                self.assertTrue(overlay.refresh())
                self.assertAlmostEqual(overlay.search(query)[0]['score'],0)
                with contextlib.closing(sqlite3.connect(source)) as db,db:db.execute('DELETE FROM media')
                while store.refresh(source):pass
                overlay.refresh()
                self.assertEqual(overlay.search(query),[])
                self.assertIn('a',overlay.snapshot.assets)
                previous=overlay.snapshot
                with store.db:
                    store.db.executemany('INSERT INTO vector_events(asset) VALUES(?)',[('deleted-b',),('deleted-c',)])
                with patch('vector_overlay.MAX_OVERLAY_ASSETS',2):
                    with self.assertRaisesRegex(RuntimeError,'compaction'):overlay.refresh()
                self.assertIs(overlay.snapshot,previous)
                self.assertEqual(set(overlay.snapshot.assets),{'a'})


if __name__=='__main__':unittest.main()
