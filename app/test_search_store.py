import contextlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from model_config import PIPELINE
from search_store import SearchStore, install_change_log,date_scope_tokens


class SearchStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root/'catalog.sqlite'
        with self.db() as db:
            db.executescript('CREATE TABLE state(id INTEGER PRIMARY KEY,identity TEXT); INSERT INTO state VALUES(1,"fixture");'
                             'CREATE TABLE media(path TEXT PRIMARY KEY,asset TEXT,payload TEXT);'
                             'CREATE TABLE index_units(asset TEXT,pipeline TEXT,stage TEXT,ordinal INTEGER,payload TEXT,PRIMARY KEY(asset,pipeline,stage,ordinal));')
        install_change_log(self.source)
        self.store = SearchStore(self.root/'search.sqlite')
        self.addCleanup(self.store.close)

    @contextlib.contextmanager
    def db(self):
        with contextlib.closing(sqlite3.connect(self.source)) as db, db:
            yield db

    def add(self, asset, date='20260928120000', text='Pe Dunăre cu capre'):
        sample = dict(path=asset, timestamp=.5, frame='/unmounted/cache/'+asset+'.jpg')
        media = dict(path=asset, url='file:///unmounted/Originals/Camera/'+asset+'.mp4', kind='video',
                     assetID=asset, frames=[sample], match=sample,
                     metadata=dict(sortDate=date, source='iCloud', camera='DJI'))
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO media VALUES(?,?,?)', (asset, asset, json.dumps(media)))
            db.execute('INSERT OR REPLACE INTO index_units VALUES(?,?,?,?,?)',
                       (asset, PIPELINE, 'speech', 0, json.dumps(dict(segments=[dict(start=2, text=text)]))))
        return media

    def drain(self):
        for _ in range(100):
            if not self.store.refresh(self.source, batch_size=2):
                return
        self.fail('Projection did not drain')

    def test_offline_sources_are_searchable_and_pages_exclude_frame_arrays(self):
        self.add('a'); self.add('b', date='20250928120000')
        self.drain()
        first = self.store.page(limit=1)
        second = self.store.page(limit=1, after=first['cursor'])
        self.assertEqual([first['items'][0]['path'], second['items'][0]['path']], ['a','b'])
        self.assertEqual(first['items'][0]['frames'], [])
        self.assertEqual(len(self.store.speech('dun', camera='DJI', day='20260928')), 1)
        self.assertEqual(self.store.speech('dun', camera='other'), [])
        self.assertEqual(self.store.speech('dun cap'), [])

    def test_updates_deletions_and_replay_replace_indexed_text(self):
        self.add('a'); self.drain()
        self.add('a', text='copii în grădină'); self.drain()
        self.assertEqual(self.store.speech('capre'), [])
        self.assertEqual(len(self.store.speech('cop')), 1)
        with self.db() as db:
            db.execute('INSERT INTO search_events(asset) VALUES(?)', ('a',))
        self.drain()
        self.assertEqual(len(self.store.speech('cop')), 1)
        with self.db() as db:
            db.execute('DELETE FROM media WHERE asset=?', ('a',))
        self.drain()
        self.assertEqual(self.store.speech('cop'), [])
        self.assertEqual(self.store.page()['items'], [])

    def test_failed_batch_keeps_previous_data_and_cursor_for_retry(self):
        self.add('a'); self.drain()
        checkpoint = tuple(self.store.db.execute('SELECT * FROM checkpoint').fetchone())
        self.add('a', text='changed')
        original = self.store._replace_asset
        def interrupted(*args):
            original(*args)
            raise RuntimeError('interrupted before cursor commit')
        with patch.object(self.store, '_replace_asset', side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                self.store.refresh(self.source)
        self.assertEqual(tuple(self.store.db.execute('SELECT * FROM checkpoint').fetchone()), checkpoint)
        self.assertEqual(len(self.store.speech('capre')), 1)
        self.drain()
        self.assertEqual(len(self.store.speech('changed')), 1)

    def test_seed_catches_concurrent_add_delete_and_update(self):
        self.add('a'); self.add('b')
        self.store.refresh(self.source, batch_size=1)
        self.add('0', text='arrival before cursor'); self.add('a', text='updated')
        with self.db() as db:
            db.execute('DELETE FROM media WHERE asset=?', ('b',))
        self.drain()
        self.assertEqual({item['path'] for item in self.store.page()['items']}, {'0','a'})
        self.assertEqual(len(self.store.speech('updated')), 1)

    def test_other_catalog_identity_is_rejected(self):
        self.add('a'); self.drain()
        with self.db() as db:
            db.execute('UPDATE state SET identity=?', ('other',))
        with self.assertRaises(ValueError):
            self.store.refresh(self.source)

    def test_restored_branch_with_reused_sequence_is_rejected(self):
        self.add('a'); self.drain()
        self.add('a', text='new'); self.drain()
        with self.db() as db:
            db.execute("UPDATE search_events SET token='other-branch' WHERE sequence=(SELECT max(sequence) FROM search_events)")
        with self.assertRaisesRegex(ValueError, 'restored'):
            self.store.refresh(self.source)

    def test_event_compaction_keeps_anchor_and_new_arrivals(self):
        self.add('a'); self.drain()
        self.add('b'); self.drain()
        self.add('c')
        self.assertGreater(self.store.compact_acknowledged_events(self.source), 0)
        self.drain()
        self.assertEqual(len(self.store.page()['items']), 3)
        self.store.compact_acknowledged_events(self.source)
        with self.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM search_events').fetchone()[0], 1)
        self.assertFalse(self.store.refresh(self.source))

    def test_reopen_serves_saved_search_without_rebuilding(self):
        self.add('a'); self.drain()
        with SearchStore(self.root/'search.sqlite') as reopened:
            with patch.object(reopened, '_replace_asset', side_effect=AssertionError('Unchanged catalog rebuilt')):
                self.assertFalse(reopened.refresh(self.source))
                self.assertEqual(len(reopened.speech('dun')), 1)
                self.assertEqual(reopened.page()['items'][0]['assetID'], 'a')

    def test_invalid_update_rolls_back_deleted_old_projection(self):
        self.add('a'); self.drain()
        with self.db() as db:
            db.execute('UPDATE media SET payload=?', ('malformed',))
        with self.assertRaises(json.JSONDecodeError):
            self.store.refresh(self.source)
        self.assertEqual(len(self.store.speech('dun')), 1)

    def test_filters_apply_before_result_limit_and_quotes_are_data(self):
        self.add('a',date='20260928120000',text='OR dunare')
        self.add('b',date='20250928120000',text='OR dunare')
        self.drain()
        self.assertEqual([h['asset'] for h in self.store.speech('" OR *',limit=1,day='20260928')], ['a'])
        with self.assertRaises(ValueError):
            self.store.speech('dun',limit=0)

    def test_legacy_seed_is_durable_and_uses_matrix_rows_without_vector_file_reads(self):
        media = self.add('a');self.drain()
        index = self.root/'legacy';index.mkdir()
        (index/'samples.json').write_text(json.dumps(dict(samples=media['frames'])))
        speech = self.root/'speech';speech.mkdir()
        (speech/'0.json').write_text(json.dumps(dict(path='a',transcript=dict(segments=[dict(start=3,text='legacy phrase')]))))
        self.assertTrue(self.store.seed_legacy(self.source,index,speech))
        self.assertFalse(self.store.seed_legacy(self.source,index,speech))
        self.assertEqual(len(self.store.speech('legacy')),1)
        row=self.store.db.execute('SELECT vector_path,vector_row FROM embeddings').fetchone()
        self.assertEqual(row['vector_row'],0)
        self.assertEqual(Path(row['vector_path']), (index/'embeddings.npy').resolve())
        self.add('a',text='new incremental phrase');self.drain()
        self.assertEqual(len(self.store.speech('legacy')),1)
        self.assertEqual(len(self.store.speech('incremental')),1)
        (index/'samples.json').write_text('{}')
        with self.assertRaises(ValueError):
            self.store.seed_legacy(self.source,index,speech)

    def test_grid_lock_cutoff_preserves_old_assets_across_metadata_updates(self):
        self.add('a');self.drain()
        cutoff=self.store.db.execute('SELECT max(sequence) FROM vector_events').fetchone()[0]
        self.add('a',text='updated old asset');self.add('b');self.drain()
        allowed=self.store.allowed_paths(dict(arrivalThrough=cutoff))
        self.assertEqual(allowed,{'a'})
        self.assertEqual(self.store.allowed_paths(dict(assets=[])),set())
        self.assertEqual(self.store.allowed_paths(dict(camera='DJI',assets=['b'])),{'b'})
        self.assertEqual([hit['asset'] for hit in self.store.speech('updated',allowed_paths=allowed)],['a'])

    def test_date_postings_cover_year_month_and_leap_day_boundaries(self):
        self.assertEqual(date_scope_tokens('20240101','20241231'),['y2024'])
        self.assertEqual(date_scope_tokens('20240201','20240229'),['m202402'])
        self.assertEqual(date_scope_tokens('20240228','20240302'),['d20240228','d20240229','d20240301','d20240302'])
        self.assertEqual(date_scope_tokens('20250102','20250101'),[])

    def test_structured_speech_scope_precedes_limit_without_global_path_materialization(self):
        self.add('a',date='20240229120000');self.add('b',date='20240301120000');self.drain()
        hits=self.store.speech('dun',limit=1,scope=dict(camera='DJI',kind='video',**{'from':'20240201','through':'20240229235959'}))
        self.assertEqual([hit['asset'] for hit in hits],['a'])
        self.assertEqual(self.store.speech('dun',scope=dict(assets=[])),[])

    def test_atlas_metadata_changes_do_not_force_vector_reloads(self):
        media=self.add('a')
        media['frames'][0]['sourceSize']=[180,320]
        with self.db() as db:
            db.execute('UPDATE media SET payload=? WHERE asset=?',(json.dumps(media),'a'))
            db.execute('INSERT INTO index_units VALUES(?,?,?,?,?)',('a',PIPELINE,'embedding',0,json.dumps(dict(vector='/offline/vector.npy',sample=media['frames'][0]))))
        self.drain()
        sequence=self.store.db.execute('SELECT max(sequence) FROM vector_events').fetchone()[0]
        packed=dict(media['frames'][0],frame='/offline/atlas.jpg',crop=[0,0,144,256])
        media.update(frames=[packed],match=packed)
        with self.db() as db:
            db.execute('UPDATE media SET payload=? WHERE asset=?',(json.dumps(media),'a'))
            db.execute("UPDATE index_units SET payload=? WHERE asset=? AND stage='embedding'",(json.dumps(dict(vector='/offline/vector.npy',sample=packed)),'a'))
        self.drain()
        self.assertEqual(self.store.db.execute('SELECT max(sequence) FROM vector_events').fetchone()[0],sequence)
        self.assertEqual(self.store.speech('dun')[0]['crop'],[0,0,144,256])

    def test_both_browse_orders_have_an_indexed_query_plan(self):
        for order in ("captured DESC,asset","(captured=''),captured,asset"):
            plan=self.store.db.execute('EXPLAIN QUERY PLAN SELECT asset,captured,summary FROM files ORDER BY '+order+' LIMIT 100').fetchall()
            self.assertFalse(any('TEMP B-TREE' in row[3] for row in plan),plan)


if __name__ == '__main__':
    unittest.main()
