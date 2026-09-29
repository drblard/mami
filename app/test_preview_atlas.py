import contextlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import shutil

try:
    from PIL import Image
except ImportError:
    Image=None

from index_store import connection,ensure_schema
from model_config import PIPELINE
from preview_atlas import pack_asset,retire_raw_frames
from search_store import SearchStore,install_change_log


@unittest.skipIf(Image is None,'Pillow checks run in the Mac environment')
class PreviewAtlasTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve();self.database=self.root/'catalog.sqlite'
        with contextlib.closing(sqlite3.connect(self.database)) as db,db:
            db.executescript('CREATE TABLE state(id INTEGER PRIMARY KEY,revision INTEGER,change_token TEXT,identity TEXT); INSERT INTO state VALUES(1,0,"initial","fixture"); CREATE TABLE media(path TEXT PRIMARY KEY,asset TEXT,payload TEXT);')
        self.asset='sha256:fixture'
        self.frames=[]
        with connection(self.database) as db:
            ensure_schema(db)
            db.execute("INSERT INTO index_jobs(asset,path,kind,signature,logical,state) VALUES(?,?,?,?,?,'complete')",(self.asset,'/offline/original.mp4','video','signature','logical'))
            for ordinal,color in enumerate(['red','green','blue']):
                path=self.root/f'frame-{ordinal}.jpg'
                Image.new('RGB',(180,320),color).save(path)
                sample=dict(path='logical',kind='video',timestamp=ordinal+.5,frame=str(path))
                self.frames.append(sample)
                db.execute('INSERT INTO index_units VALUES(?,?,?,?,?)',(self.asset,PIPELINE,'frame',ordinal,json.dumps(sample)))
                db.execute('INSERT INTO index_units VALUES(?,?,?,?,?)',(self.asset,PIPELINE,'embedding',ordinal,json.dumps(dict(sample=sample,vector=f'/offline/vector-{ordinal}.npy'))))
            db.execute('INSERT INTO media VALUES(?,?,?)',('/offline/original.mp4',self.asset,json.dumps(dict(path='logical',kind='video',url='file:///offline/original.mp4',assetID=self.asset,frames=self.frames,match=self.frames[0]))))

    def test_offline_pack_preserves_times_vectors_orientation_and_source_cache(self):
        manifest=pack_asset(self.database,self.asset,self.root/'artifacts')
        self.assertEqual(len(manifest['frames']),3)
        with connection(self.database) as db:
            updated=json.loads(db.execute('SELECT payload FROM media').fetchone()[0])
            for ordinal,frame in enumerate(updated['frames']):
                self.assertEqual(frame['timestamp'],ordinal+.5)
                x,y,width,height=frame['crop']
                self.assertLess(width,height)
                with Image.open(frame['frame']) as sheet:
                    pixel=sheet.crop((x,y,x+width,y+height)).getpixel((width//2,height//2))
                    self.assertEqual(max(range(3),key=lambda channel:pixel[channel]),ordinal)
                vector=json.loads(db.execute("SELECT payload FROM index_units WHERE stage='embedding' AND ordinal=?",(ordinal,)).fetchone()[0])
                self.assertEqual(vector['vector'],f'/offline/vector-{ordinal}.npy')
                self.assertEqual(vector['sample'],frame)
        self.assertTrue(all(Path(frame['frame']).exists() for frame in self.frames))
        self.assertIsNone(pack_asset(self.database,self.asset,self.root/'artifacts'))

    def test_incomplete_ai_job_is_not_packed(self):
        with connection(self.database) as db:db.execute("UPDATE index_jobs SET state='running'")
        self.assertIsNone(pack_asset(self.database,self.asset,self.root/'artifacts'))

    def test_changed_frames_abort_publication(self):
        calls=0
        def changed():
            nonlocal calls
            calls+=1
            if calls==2:
                with connection(self.database) as db:db.execute("DELETE FROM index_units WHERE stage='frame' AND ordinal=2")
        self.assertIsNone(pack_asset(self.database,self.asset,self.root/'artifacts',checkpoint=changed))
        with connection(self.database) as db:
            current=json.loads(db.execute('SELECT payload FROM media').fetchone()[0])
            self.assertNotIn('crop',current['frames'][0])

    def own_raw_frames(self):
        artifacts=self.root/'artifacts'
        folder=artifacts/self.asset.removeprefix('sha256:');folder.mkdir(parents=True)
        with connection(self.database) as db:
            for ordinal,frame in enumerate(self.frames):
                target=folder/Path(frame['frame']).name
                shutil.copy2(frame['frame'],target)
                frame['frame']=str(target)
                db.execute("UPDATE index_units SET payload=? WHERE stage='frame' AND ordinal=?",(json.dumps(frame),ordinal))
            value=json.loads(db.execute('SELECT payload FROM media').fetchone()[0])
            value.update(frames=self.frames,match=self.frames[0])
            db.execute('UPDATE media SET payload=?',(json.dumps(value),))
        return artifacts

    def test_retirement_waits_for_projection_and_preserves_changed_or_external_files(self):
        artifacts=self.own_raw_frames()
        install_change_log(self.database)
        with SearchStore(self.root/'search.sqlite') as store:
            while store.refresh(self.database):pass
            pack_asset(self.database,self.asset,artifacts)
            self.assertIsNone(retire_raw_frames(self.database,self.asset,artifacts,self.root/'search.sqlite'))
            while store.refresh(self.database):pass
            changed=Path(self.frames[0]['frame']);changed.write_bytes(b'user-changed cache')
            self.assertEqual(retire_raw_frames(self.database,self.asset,artifacts,self.root/'search.sqlite'),2)
            self.assertTrue(changed.exists())
            self.assertTrue((self.root/'frame-1.jpg').exists())
            self.assertEqual(retire_raw_frames(self.database,self.asset,artifacts,self.root/'search.sqlite'),0)

    def test_corrupt_packed_sheet_never_authorizes_raw_retirement(self):
        artifacts=self.own_raw_frames()
        install_change_log(self.database)
        manifest=pack_asset(self.database,self.asset,artifacts)
        with SearchStore(self.root/'search.sqlite') as store:
            while store.refresh(self.database):pass
            Path(manifest['frames'][0]['sample']['frame']).write_bytes(b'corrupted')
            with self.assertRaisesRegex(ValueError,'checksum'):
                retire_raw_frames(self.database,self.asset,artifacts,self.root/'search.sqlite')
        self.assertTrue(all(Path(frame['frame']).exists() for frame in self.frames))


if __name__=='__main__':unittest.main()
