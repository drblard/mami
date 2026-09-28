import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from prune_catalog_backups import plan, apply


def snapshot(root, revision, age, label='one', identity='fixture'):
    name=f'catalog-{identity}-r{revision}.sqlite'
    with sqlite3.connect(root/name) as db:
        db.executescript('PRAGMA user_version=2; CREATE TABLE state(id INTEGER PRIMARY KEY,identity TEXT,revision INTEGER,change_token TEXT);'
                         'CREATE TABLE annotations(asset TEXT PRIMARY KEY,payload TEXT);'
                         'CREATE TABLE annotation_history(id INTEGER PRIMARY KEY,asset TEXT,payload TEXT);')
        db.execute('INSERT INTO state VALUES(1,?,?,?)',(identity,revision,str(revision)))
        db.execute('INSERT INTO annotations VALUES(?,?)',('asset',label))
    db.close()
    receipt=dict(file=name,identity=identity,revision=str(revision),changeToken=str(revision))
    (root/f'{identity}-{revision}.json').write_text(json.dumps(receipt))
    stamp=time.time()-age
    os.utime(root/name,(stamp,stamp))
    return name


class RetentionTests(unittest.TestCase):
    def test_keeps_manual_states_baseline_and_recent_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            baseline=snapshot(root,1,4000)
            edit=snapshot(root,2,3990,label='unique edit')
            redundant=snapshot(root,3,3980)
            for i in range(4,10):snapshot(root,i,3970-i)
            value=plan(root)
            self.assertIn(baseline,value['keep'])
            self.assertIn(edit,value['keep'])
            self.assertIn(redundant,value['remove'])
            self.assertTrue(all(f'catalog-fixture-r{i}.sqlite' in value['keep'] for i in (7,8,9)))

    def test_changed_or_unknown_files_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for i in range(1,10):snapshot(root,i,4000-i)
            (root/'unknown.sqlite').write_bytes(b'not a completed snapshot')
            value=plan(root)
            changed=root/value['remove'][0]
            with changed.open('ab') as f:f.write(b'changed after planning')
            output=root/'audit';output.mkdir()
            result=apply(value,output)
            self.assertTrue(changed.exists())
            self.assertTrue((root/'unknown.sqlite').exists())
            self.assertTrue(result['skipped'])
            self.assertTrue(all((root/name).exists() for name in value['keep']))

    def test_bad_retained_snapshot_aborts_before_any_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for i in range(1,10):snapshot(root,i,4000-i)
            value=plan(root)
            (root/value['keep'][0]).write_bytes(b'broken')
            output=root/'audit';output.mkdir()
            with self.assertRaises(ValueError):apply(value,output)
            self.assertTrue(all((root/name).exists() for name in value['remove']))


if __name__=='__main__':unittest.main()
