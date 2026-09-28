import unittest
import contextlib
import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch
from search_worker import Snapshot, CatalogIndex
from search_logic import SpeechIndex, combine_hits

try:
    import numpy as np
except ImportError:
    np = None


class SpeechSearchTests(unittest.TestCase):
    def test_format_scope_is_applied_before_speech_limit(self):
        frames = {f'file-{i}': [dict(path=f'file-{i}', timestamp=0, frame='cached')] for i in range(80)}
        segments = [(path, dict(text='capre', start=0)) for path in frames]
        allowed = {'file-79'}
        index = SpeechIndex(segments, frames)
        self.assertEqual([h['path'] for h in index.search('capre', allowed)], ['file-79'])
        self.assertEqual(index.search('capre', set()), [])

    def test_both_fuses_ranks_and_preserves_spoken_moment(self):
        visual = [{'path': 'visual-only', 'score': .9, 'timestamp': 1},
                  {'path': 'shared', 'score': .3, 'timestamp': 50}]
        spoken = [{'path': 'shared', 'score': 999, 'timestamp': 12.4, 'evidence': 'Dunăre'},
                  {'path': 'speech-only', 'score': 2, 'timestamp': 3, 'evidence': 'Dunăre'}]
        hits = combine_hits(visual, spoken)
        self.assertEqual(len(hits), 3)
        self.assertEqual(hits[0]['path'], 'shared')
        self.assertEqual(hits[0]['timestamp'], 12.4)
        self.assertEqual(hits[0]['evidence'], 'Dunăre')
        self.assertLess(hits[0]['score'], 1)

    def test_both_handles_one_empty_source_and_caps_results(self):
        hits = [{'path': f'file-{i}', 'timestamp': i} for i in range(80)]
        self.assertEqual([h['path'] for h in combine_hits(hits, [])], [h['path'] for h in hits[:60]])
        self.assertEqual(combine_hits([], []), [])

    def test_accents_and_exact_words_with_segment_timestamp(self):
        frames = {'video': [{'path': 'video', 'timestamp': .5, 'frame': 'a'},
                            {'path': 'video', 'timestamp': 8.5, 'frame': 'b'}]}
        segments = [('video', {'text': 'Am ajuns la Dunăre!', 'start': 8.1})]
        index = SpeechIndex(segments, frames)
        hit = index.search('dunare')[0]
        self.assertEqual(hit['frame'], 'b')
        self.assertEqual(hit['timestamp'], 8.1)
        self.assertEqual(hit['evidence'], 'Am ajuns la Dunăre!')
        self.assertEqual(index.search('dun')[0]['path'], 'video')
        self.assertEqual(index.search('dunare capre'), [])
        self.assertEqual(index.search('dun '), [])

    def test_duplicate_file_and_missing_file(self):
        frames = {'video': [{'path': 'video', 'timestamp': .5, 'frame': 'a'}]}
        segments = [('video', {'text': 'capre', 'start': 0}),
                    ('video', {'text': 'alte capre', 'start': 2}),
                    ('missing', {'text': 'capre', 'start': 0})]
        index = SpeechIndex(segments, frames)
        self.assertEqual(len(index.search('capre')), 1)
        self.assertEqual(index.search('???'), [])

    def test_nearest_frame_at_boundaries_and_ties(self):
        frames = {'video': [dict(path='video', timestamp=9, frame='late'),
                            dict(path='video', timestamp=1, frame='early')]}
        for start, expected in [(0, 'early'), (5, 'early'), (8, 'late'), (20, 'late')]:
            index = SpeechIndex([('video', dict(text='capre', start=start))], frames)
            self.assertEqual(index.search('cap')[0]['frame'], expected)


@unittest.skipIf(np is None, 'NumPy is required for real search-index checks')
class IndexTests(unittest.TestCase):
    def test_file_ranking_matches_exhaustive_search_and_filters_before_limit(self):
        rng = np.random.default_rng(42)
        samples = [dict(path=f'file-{i % 80}', timestamp=i, frame=str(i)) for i in range(1000)]
        vectors = rng.random((1000, 8), dtype=np.float32)
        feature = rng.random(8, dtype=np.float32)
        index = Snapshot(samples, vectors, [])
        for allowed in (None, {'file-79'}, set()):
            expected, seen = [], set()
            for i in np.argsort(-(vectors @ feature)):
                sample = samples[i]
                if sample['path'] in seen or (allowed is not None and sample['path'] not in allowed):
                    continue
                expected.append(sample['frame'])
                seen.add(sample['path'])
                if len(expected) == 60:
                    break
            self.assertEqual([hit['frame'] for hit in index.visual(feature, allowed)], expected)

    def test_speech_prefix_accents_and_completed_words(self):
        samples = [dict(path='a', timestamp=0, frame='a'), dict(path='b', timestamp=10, frame='b')]
        segments = [('a', dict(text='Pe Dunăre cu capre', start=2)),
                    ('b', dict(text='Pe Dunărea albastră', start=11))]
        index = Snapshot(samples, np.eye(2), segments)
        self.assertEqual(len(index.spoken('dun')), 2)
        self.assertEqual([hit['path'] for hit in index.spoken('dunare cap')], ['a'])
        self.assertEqual(index.spoken('dun cap'), [])
        self.assertEqual([hit['path'] for hit in index.spoken('dun', {'b'})], ['b'])
        self.assertEqual(index.spoken('???'), [])

    def test_refresh_reuses_vectors_handles_replacement_and_removal(self):
        from index_queue import PIPELINE
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / 'catalog.sqlite'
            frame = root / 'frame.jpg'
            frame.touch()
            first, second = root / 'first.npy', root / 'second.npy'
            np.save(first, np.array([1, 0], dtype=np.float32))
            np.save(second, np.array([0, 1], dtype=np.float32))
            with contextlib.closing(sqlite3.connect(database)) as db, db:
                db.executescript('CREATE TABLE state(change_token TEXT); INSERT INTO state VALUES("1"); '
                                 'CREATE TABLE media(asset TEXT); INSERT INTO media VALUES("asset"); '
                                 'CREATE TABLE index_units(asset TEXT,pipeline TEXT,stage TEXT,ordinal INTEGER,payload TEXT);')
                payload = dict(vector=str(first), sample=dict(path='new', timestamp=0, frame=str(frame)))
                db.execute('INSERT INTO index_units VALUES(?,?,?,?,?)', ('asset', PIPELINE, 'embedding', 0, json.dumps(payload)))
            index = CatalogIndex([], np.empty((0, 2)), [], database)
            with patch('numpy.load', wraps=np.load) as load:
                self.assertTrue(index.refresh())
                old = index.snapshot
                self.assertEqual(load.call_count, 1)
                with contextlib.closing(sqlite3.connect(database)) as db, db:
                    db.execute('UPDATE state SET change_token="2"')
                self.assertFalse(index.refresh())
                self.assertIs(index.snapshot, old)
                self.assertEqual(load.call_count, 1)
                with contextlib.closing(sqlite3.connect(database)) as db, db:
                    payload['vector'] = str(second)
                    db.execute('UPDATE index_units SET payload=?', (json.dumps(payload),))
                    db.execute('UPDATE state SET change_token="3"')
                self.assertTrue(index.refresh())
                self.assertEqual(load.call_count, 2)
                self.assertEqual(old.visual(np.array([1, 0]))[0]['score'], 1)
                self.assertEqual(index.snapshot.visual(np.array([1, 0]))[0]['score'], 0)
                with contextlib.closing(sqlite3.connect(database)) as db, db:
                    db.execute('DELETE FROM media')
                    db.execute('UPDATE state SET change_token="4"')
                self.assertTrue(index.refresh())
                self.assertEqual(index.snapshot.visual(np.array([1, 0])), [])
                self.assertEqual(load.call_count, 2)


if __name__ == '__main__':
    unittest.main()
