import contextlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import shutil

try:
    from PIL import Image
except ImportError:
    Image=None

from index_store import connection,ensure_schema
from model_config import PIPELINE
from preview_atlas import ensure_pack_schema,pack_asset,retire_raw_frames
from preview_cache_worker import check_backfill_priority
from preview_retention import prune_generations
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

    def test_frame_repairs_invalidate_completion_errors_and_retirement(self):
        ensure_pack_schema(self.database)
        operations=[
            ('insert',"INSERT INTO index_units VALUES(?,?,?,?,?)",(self.asset,PIPELINE,'frame',3,json.dumps(self.frames[0]))),
            ('update',"UPDATE index_units SET payload=? WHERE stage='frame' AND ordinal=0",(json.dumps(dict(self.frames[0],timestamp=9)),)),
            ('delete',"DELETE FROM index_units WHERE stage='frame' AND ordinal=3",()),
        ]
        for name,sql,parameters in operations:
            with self.subTest(operation=name),connection(self.database) as db:
                db.execute('INSERT INTO preview_packs VALUES(?,?,?)',(self.asset,1,'old-pack'))
                db.execute('INSERT INTO preview_pack_errors VALUES(?,?,?)',(self.asset,'old failure',3))
                db.execute('INSERT INTO preview_retired VALUES(?,?)',(self.asset,'old-pack'))
                db.execute(sql,parameters)
                for table in ('preview_packs','preview_pack_errors','preview_retired'):
                    self.assertEqual(db.execute('SELECT count(*) FROM '+table).fetchone()[0],0)

    def run_worker(self, once=True):
        command=[sys.executable,'-B',str(Path(__file__).with_name('preview_cache_worker.py')),
                 '--database',str(self.database),'--artifacts',str(self.root/'artifacts')]
        if once:command.append('--once')
        return subprocess.run(command,input='',capture_output=True,text=True,timeout=10)

    def test_worker_eof_stops_without_waiting_for_poll_interval(self):
        result=self.run_worker(once=False)
        self.assertEqual(result.returncode,0,result.stderr)
        with connection(self.database) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM preview_packs').fetchone()[0],0)

    def test_retryable_preview_error_takes_priority_over_backfill(self):
        stop,busy=threading.Event(),threading.Event()
        with connection(self.database) as db:
            db.execute("UPDATE preview_jobs SET state='error',attempts=1")
        with self.assertRaisesRegex(InterruptedError,'pending previews'):
            check_backfill_priority(self.database,stop,busy)
        with connection(self.database) as db:db.execute('UPDATE preview_jobs SET attempts=3')
        check_backfill_priority(self.database,stop,busy)

    def test_worker_retry_limit_and_repaired_frame_restart(self):
        with connection(self.database) as db:db.execute("UPDATE preview_jobs SET state='complete'")
        missing=Path(self.frames[0]['frame'])
        saved=missing.read_bytes();missing.unlink()
        for attempt in range(1,4):
            result=self.run_worker()
            self.assertNotEqual(result.returncode,0)
            with connection(self.database) as db:
                self.assertEqual(db.execute('SELECT attempts FROM preview_pack_errors').fetchone()[0],attempt)
                self.assertEqual(db.execute('SELECT count(*) FROM preview_packs').fetchone()[0],0)
        exhausted=self.run_worker()
        self.assertEqual(exhausted.returncode,0,exhausted.stderr)
        self.assertEqual(exhausted.stdout,'')
        missing.write_bytes(saved)
        with connection(self.database) as db:
            db.execute("UPDATE index_units SET payload=? WHERE stage='frame' AND ordinal=0",(json.dumps(self.frames[0]),))
        recovered=self.run_worker()
        self.assertEqual(recovered.returncode,0,recovered.stderr)
        self.assertTrue(any(json.loads(line).get('changed') for line in recovered.stdout.splitlines()))
        with connection(self.database) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM preview_pack_errors').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM preview_packs').fetchone()[0],1)

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

    def test_backfill_yields_before_publication_and_can_resume(self):
        artifacts=self.root/'artifacts'
        for reason in ('stop','editor','preview'):
            with self.subTest(reason=reason):
                stop,busy=threading.Event(),threading.Event()
                with connection(self.database) as db:db.execute("UPDATE preview_jobs SET state='complete'")
                calls=0
                def checkpoint():
                    nonlocal calls
                    calls+=1
                    if calls==2:
                        if reason=='stop':stop.set()
                        elif reason=='editor':busy.set()
                        else:
                            with connection(self.database) as db:db.execute("UPDATE preview_jobs SET state='queued'")
                    check_backfill_priority(self.database,stop,busy)
                with self.assertRaises(InterruptedError):
                    pack_asset(self.database,self.asset,artifacts,checkpoint=checkpoint)
                self.assertEqual(list((artifacts/'packed-previews').iterdir()),[])
                with connection(self.database) as db:
                    value=json.loads(db.execute('SELECT payload FROM media').fetchone()[0])
                    self.assertEqual(value['frames'],self.frames)
                    self.assertEqual(db.execute('SELECT count(*) FROM preview_packs').fetchone()[0],0)
                self.assertTrue(all(Path(frame['frame']).exists() for frame in self.frames))
        with connection(self.database) as db:db.execute("UPDATE preview_jobs SET state='complete'")
        manifest=pack_asset(self.database,self.asset,artifacts)
        self.assertEqual(len(manifest['frames']),3)

    def test_publication_database_failure_preserves_raw_frames_and_retry(self):
        artifacts=self.root/'artifacts'
        with connection(self.database) as db:
            db.execute("CREATE TRIGGER fail_pack BEFORE UPDATE ON index_units WHEN NEW.stage='frame' BEGIN SELECT RAISE(ABORT,'injected publication failure'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError,'injected publication failure'):
            pack_asset(self.database,self.asset,artifacts)
        with connection(self.database) as db:
            self.assertEqual(json.loads(db.execute('SELECT payload FROM media').fetchone()[0])['frames'],self.frames)
            self.assertEqual(db.execute('SELECT count(*) FROM preview_packs').fetchone()[0],0)
            db.execute('DROP TRIGGER fail_pack')
        orphan_directories=list((artifacts/'packed-previews').iterdir())
        self.assertEqual(len(orphan_directories),1)
        self.assertTrue((orphan_directories[0]/'manifest.json').is_file())
        self.assertTrue(all(Path(frame['frame']).exists() for frame in self.frames))
        manifest=pack_asset(self.database,self.asset,artifacts)
        self.assertNotEqual(Path(manifest['frames'][0]['sample']['frame']).parent,orphan_directories[0])
        self.assertTrue(all(Path(frame['sample']['frame']).is_file() for frame in manifest['frames']))

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

    def test_incorrect_projected_crop_or_timestamp_prevents_raw_retirement(self):
        artifacts=self.own_raw_frames()
        install_change_log(self.database)
        pack_asset(self.database,self.asset,artifacts)
        with SearchStore(self.root/'search.sqlite') as store:
            while store.refresh(self.database):pass
            original=tuple(store.db.execute('SELECT crop,timestamp FROM frames WHERE ordinal=0').fetchone())
            for crop,timestamp in [('[9000,0,1,1]',original[1]),(original[0],99)]:
                with self.subTest(crop=crop,timestamp=timestamp):
                    with store.db:store.db.execute('UPDATE frames SET crop=?,timestamp=? WHERE ordinal=0',(crop,timestamp))
                    self.assertIsNone(retire_raw_frames(self.database,self.asset,artifacts,self.root/'search.sqlite'))
                    self.assertTrue(all(Path(frame['frame']).exists() for frame in self.frames))
            with store.db:store.db.execute('UPDATE frames SET crop=?,timestamp=? WHERE ordinal=0',original)
            self.assertEqual(retire_raw_frames(self.database,self.asset,artifacts,self.root/'search.sqlite'),3)

    def repack_fixture(self,artifacts):
        with connection(self.database) as db:
            for ordinal,sample in enumerate(self.frames):
                db.execute("UPDATE index_units SET payload=? WHERE stage='frame' AND ordinal=?",(json.dumps(sample),ordinal))
            media=json.loads(db.execute('SELECT payload FROM media').fetchone()[0])
            media.update(frames=self.frames,match=self.frames[0])
            db.execute('UPDATE media SET payload=?',(json.dumps(media),))
        manifest=pack_asset(self.database,self.asset,artifacts)
        return Path(manifest['frames'][0]['sample']['frame']).parent

    def test_generation_cleanup_pins_projection_current_and_one_fallback(self):
        artifacts=self.root/'artifacts'
        install_change_log(self.database)
        first=self.repack_fixture(artifacts)
        with SearchStore(self.root/'search.sqlite') as store:
            while store.refresh(self.database):pass
            second=self.repack_fixture(artifacts)
            current=self.repack_fixture(artifacts)
            unknown=artifacts/'packed-previews'/'user-notes';unknown.mkdir()
            (unknown/'keep.txt').write_text('unfamiliar work')
            self.assertEqual(prune_generations(self.database,artifacts,self.root/'search.sqlite'),0)
            while store.refresh(self.database):pass
            self.assertEqual(prune_generations(self.database,artifacts,self.root/'search.sqlite'),1)
            self.assertFalse(first.exists())
            self.assertTrue(second.exists());self.assertTrue(current.exists())
            self.assertTrue((unknown/'keep.txt').exists())
            self.assertEqual(prune_generations(self.database,artifacts,self.root/'search.sqlite'),0)

    def test_generation_cleanup_preserves_modified_registered_directory(self):
        artifacts=self.root/'artifacts'
        install_change_log(self.database)
        first=self.repack_fixture(artifacts)
        self.repack_fixture(artifacts);self.repack_fixture(artifacts)
        (first/'user-note.txt').write_text('preserve me')
        with SearchStore(self.root/'search.sqlite') as store:
            while store.refresh(self.database):pass
            self.assertEqual(prune_generations(self.database,artifacts,self.root/'search.sqlite'),0)
        self.assertEqual((first/'user-note.txt').read_text(),'preserve me')
        with connection(self.database) as db:
            self.assertEqual(db.execute('SELECT review FROM preview_generations WHERE directory=?',(str(first),)).fetchone()[0],
                             'Changed or unfamiliar files retained')

    def test_generation_cleanup_recovers_registered_publication_orphans(self):
        artifacts=self.root/'artifacts'
        install_change_log(self.database)
        with connection(self.database) as db:
            db.execute("CREATE TRIGGER fail_pack BEFORE UPDATE ON index_units WHEN NEW.stage='frame' BEGIN SELECT RAISE(ABORT,'injected failure'); END")
        for _ in range(3):
            with self.assertRaises(sqlite3.IntegrityError):pack_asset(self.database,self.asset,artifacts)
        with SearchStore(self.root/'search.sqlite') as store:
            while store.refresh(self.database):pass
            self.assertEqual(prune_generations(self.database,artifacts,self.root/'search.sqlite'),2)
        self.assertEqual(len(list((artifacts/'packed-previews').iterdir())),1)
        self.assertTrue(all(Path(frame['frame']).exists() for frame in self.frames))


if __name__=='__main__':unittest.main()
