import contextlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from storage_migration import migrate,digest


class StorageMigrationTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.lab=self.root/'lab';self.target=self.root/'support/Mami'
        self.source=self.lab/'catalog/database';self.source.mkdir(parents=True)
        self.original=self.root/'original.mp4';self.original.write_bytes(b'original media')
        artifacts=self.lab/'index-artifacts';artifacts.mkdir()
        (artifacts/'frame.jpg').write_bytes(b'cached frame');(artifacts/'vector.npy').write_bytes(b'vector')
        visual=self.lab/'runs/visual';visual.mkdir(parents=True)
        speech=self.lab/'runs/speech';speech.mkdir()
        old_frame=self.lab/'runs/older/frames/a.jpg';old_frame.parent.mkdir(parents=True);old_frame.write_bytes(b'legacy frame')
        self.sample=dict(path='logical',kind='video',frame=str(artifacts/'frame.jpg'),timestamp=.5)
        (visual/'samples.json').write_text(json.dumps(dict(root=str(self.root),samples=[dict(self.sample,frame=str(old_frame))])))
        (speech/'segments.json').write_text(json.dumps(dict(segments=[])))
        self.configuration=dict(index=str(visual),speech=str(speech))
        with contextlib.closing(sqlite3.connect(self.source/'catalog.sqlite')) as db,db:
            db.executescript('CREATE TABLE state(id INTEGER,identity TEXT,revision INTEGER,change_token TEXT); INSERT INTO state VALUES(1,"fixture",1,"token");'
                             'CREATE TABLE media(path TEXT PRIMARY KEY,asset TEXT,payload TEXT); CREATE TABLE index_units(asset TEXT,stage TEXT,payload TEXT);'
                             'CREATE TABLE seed_sources(source TEXT PRIMARY KEY);')
            db.execute('INSERT INTO media VALUES(?,?,?)',(str(self.original),'asset',json.dumps(dict(url=self.original.as_uri(),frames=[self.sample],match=self.sample))))
            db.execute('INSERT INTO index_units VALUES(?,?,?)',('asset','embedding',json.dumps(dict(vector=str(artifacts/'vector.npy'),sample=self.sample))))
            db.execute('INSERT INTO seed_sources VALUES(?)',(str(visual)+':seed',))
        with contextlib.closing(sqlite3.connect(self.source/'user.sqlite')) as db,db:
            db.executescript('PRAGMA user_version=1; CREATE TABLE state(id INTEGER,identity TEXT,revision INTEGER,change_token TEXT); INSERT INTO state VALUES(1,"fixture",12,"personal-token");'
                             'CREATE TABLE user_store_info(version INTEGER,source_identity TEXT); INSERT INTO user_store_info VALUES(1,"fixture");'
                             'CREATE TABLE annotations(asset TEXT PRIMARY KEY,payload TEXT); INSERT INTO annotations VALUES("asset","favorite");'
                             'CREATE TABLE clip_selection(id INTEGER PRIMARY KEY,payload TEXT);'
                             'CREATE TRIGGER clips_changed AFTER UPDATE ON clip_selection BEGIN UPDATE state SET revision=revision+1,change_token="changed"; END;')
            selection=[dict(assetID='asset',timestamp=3.5,url=self.original.as_uri(),frame=str(artifacts/'retired.jpg'))]
            db.execute('INSERT INTO clip_selection VALUES(1,?)',(json.dumps(selection),))
        model=self.lab/'cache/huggingface/hub/models--fixture--model/snapshots/pinned';model.mkdir(parents=True)
        (model/'weights.bin').write_bytes(b'model')
        self.models=patch('storage_migration.MODELS',dict(visual=('fixture/model','pinned')))
        self.models.start();self.addCleanup(self.models.stop)

    def test_publishes_verified_data_and_preserves_personal_meaning_and_original(self):
        before=digest(self.source/'user.sqlite');original=digest(self.original)
        result=migrate(self.lab,self.target,self.configuration)
        self.assertEqual(result['identity'],'fixture')
        self.assertEqual(digest(self.source/'user.sqlite'),before)
        self.assertEqual(digest(self.original),original)
        with contextlib.closing(sqlite3.connect(self.target/'Personal/user.sqlite')) as db:
            self.assertEqual(db.execute('SELECT revision,change_token FROM state').fetchone(),(12,'personal-token'))
            self.assertEqual(db.execute('SELECT * FROM annotations').fetchall(),[('asset','favorite')])
            selected=json.loads(db.execute('SELECT payload FROM clip_selection').fetchone()[0])[0]
            self.assertEqual(selected['timestamp'],3.5)
            self.assertEqual(selected['url'],self.original.as_uri())
            self.assertEqual(selected['frame'],str(self.target/'Derived/Artifacts/retired.jpg'))
        with contextlib.closing(sqlite3.connect(self.target/'Derived/Catalog/catalog.sqlite')) as db:
            media=json.loads(db.execute('SELECT payload FROM media').fetchone()[0])
            self.assertEqual(Path(media['match']['frame']).read_bytes(),b'cached frame')
            self.assertEqual(media['url'],self.original.as_uri())
            self.assertEqual(db.execute('SELECT source FROM seed_sources').fetchone()[0],str(self.target/'Derived/Legacy/Visual')+':seed')
        legacy=json.loads((self.target/'Derived/Legacy/Visual/samples.json').read_text())
        self.assertEqual(Path(legacy['samples'][0]['frame']).read_bytes(),b'legacy frame')
        self.assertEqual(migrate(self.lab,self.target,self.configuration),result)

    def test_interruption_does_not_publish_or_modify_authoritative_source(self):
        before=digest(self.source/'user.sqlite')
        def interrupt(stage):
            if stage=='references':raise RuntimeError('injected interruption')
        with self.assertRaisesRegex(RuntimeError,'injected'):
            migrate(self.lab,self.target,self.configuration,checkpoint=interrupt)
        self.assertFalse(self.target.exists())
        self.assertEqual(digest(self.source/'user.sqlite'),before)
        self.assertEqual(len(list(self.target.parent.glob('Mami.migrating-*'))),1)
        self.assertEqual(migrate(self.lab,self.target,self.configuration)['phase'],'data-published')

    def test_existing_destination_is_never_overwritten(self):
        self.target.mkdir(parents=True);(self.target/'notes.txt').write_text('user work')
        with self.assertRaises(FileExistsError):migrate(self.lab,self.target,self.configuration)
        self.assertEqual((self.target/'notes.txt').read_text(),'user work')

    def test_missing_personal_store_never_uses_the_catalog_mirror(self):
        (self.source/'user.sqlite').unlink()
        with self.assertRaisesRegex(FileNotFoundError,'Authoritative'):
            migrate(self.lab,self.target,self.configuration)
        self.assertFalse(self.target.exists())

    def test_destination_created_during_copy_is_not_clobbered(self):
        def race(stage):
            if stage=='publication':
                self.target.mkdir();(self.target/'notes.txt').write_text('new user work')
        with self.assertRaises(OSError):migrate(self.lab,self.target,self.configuration,checkpoint=race)
        self.assertEqual((self.target/'notes.txt').read_text(),'new user work')

    def test_missing_published_personal_data_is_not_recreated_from_old_lab(self):
        migrate(self.lab,self.target,self.configuration)
        (self.target/'Personal/user.sqlite').unlink()
        with self.assertRaisesRegex(FileNotFoundError,'Published personal database'):
            migrate(self.lab,self.target,self.configuration)
        self.assertFalse((self.target/'Personal/user.sqlite').exists())

    def test_path_relocation_preserves_completed_pack_checkpoints_and_triggers(self):
        directory=self.lab/'index-artifacts/packed-previews/fixture'
        with contextlib.closing(sqlite3.connect(self.source/'catalog.sqlite')) as db,db:
            db.executescript("CREATE TABLE preview_packs(asset TEXT PRIMARY KEY,version INTEGER,directory TEXT); CREATE TRIGGER preview_pack_invalidate_UPDATE AFTER UPDATE ON index_units WHEN NEW.stage='frame' BEGIN DELETE FROM preview_packs WHERE asset=NEW.asset; END;")
            db.execute('INSERT INTO preview_packs VALUES(?,?,?)',('asset',1,str(directory)))
            db.execute('INSERT INTO index_units VALUES(?,?,?)',('asset','frame',json.dumps(self.sample)))
        migrate(self.lab,self.target,self.configuration)
        with contextlib.closing(sqlite3.connect(self.target/'Derived/Catalog/catalog.sqlite')) as db,db:
            self.assertEqual(db.execute('SELECT directory FROM preview_packs').fetchone()[0],str(self.target/'Derived/Artifacts/packed-previews/fixture'))
            db.execute("UPDATE index_units SET payload=payload WHERE stage='frame'")
            self.assertEqual(db.execute('SELECT count(*) FROM preview_packs').fetchone()[0],0)


if __name__=='__main__':unittest.main()
