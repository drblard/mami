import contextlib
import tempfile
import unittest
from pathlib import Path

try:
    import numpy as np
    import faces_store
    import face_assignments
except ImportError:
    np = None

DIMENSIONS = 512


def unit(vector):
    return vector / np.linalg.norm(vector, axis=-1, keepdims=True)


@unittest.skipIf(np is None, 'NumPy face checks run where the worker runtime is installed')
class FacesStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'Faces' / 'faces.sqlite'
        self.random = np.random.default_rng(7)
        self.people = unit(self.random.normal(size=(3, DIMENSIONS)))

    def tearDown(self):
        self.directory.cleanup()

    def embedding(self, person):
        return unit(self.people[person] + self.random.normal(size=DIMENSIONS) * 0.6 / np.sqrt(DIMENSIONS))

    def face(self, person, box=(0.1, 0.1, 0.3, 0.4), timestamp=None, track=0, representative=True, reliable=True):
        return dict(timestamp=timestamp, box=box, score=0.9, eye_distance=40, frontalness=0.9, sharpness=100, norm=20,
                    track=track, representative=representative, reliable=reliable, embedding=self.embedding(person))

    def publish(self, db, asset, faces, kind='image'):
        faces_store.sync_jobs(db, self.catalog + [(asset, f'/media/{asset}', kind, 'sig', None, 'camera')], 'p1')
        self.catalog.append((asset, f'/media/{asset}', kind, 'sig', None, 'camera'))
        self.assertTrue(faces_store.publish_faces(db, asset, 'sig', faces))

    def test_job_sync_adds_requeues_changed_originals_and_removes_deleted(self):
        with faces_store.connection(self.path) as db:
            rows = [('a', '/m/a.jpg', 'image', 's1', 2.0, 'camera'), ('b', '/m/b.mov', 'video', 's1', 1.0, 'other')]
            self.assertEqual(faces_store.sync_jobs(db, rows, 'p1'), (2, 0, 0))
            self.assertEqual(faces_store.next_job(db)['asset'], 'a')
            self.assertTrue(faces_store.publish_faces(db, 'a', 's1', [self.face(0)]))
            self.assertEqual(faces_store.counts(db), dict(remaining=1, completed=1, failed=0))
            moved = [('a', '/n/a.jpg', 'image', 's1', 2.0, 'camera'), ('b', '/m/b.mov', 'video', 's1', 1.0, 'screen')]
            self.assertEqual(faces_store.sync_jobs(db, moved, 'p1'), (0, 0, 0))
            self.assertEqual(db.execute("SELECT path,state FROM face_jobs WHERE asset='a'").fetchone()[:], ('/n/a.jpg', 'complete'))
            self.assertEqual(db.execute("SELECT origin FROM face_jobs WHERE asset='b'").fetchone()[0], 'screen')
            changed = [('a', '/n/a.jpg', 'image', 's2', 2.0, 'camera')]
            self.assertEqual(faces_store.sync_jobs(db, changed, 'p1'), (0, 1, 1))
            self.assertEqual(db.execute('SELECT count(*) FROM faces').fetchone()[0], 0)
            self.assertEqual([row[0] for row in db.execute('SELECT asset FROM face_jobs')], ['a'])
            self.assertEqual(faces_store.sync_jobs(db, changed, 'p2'), (0, 1, 0))

    def test_stale_extraction_is_not_published(self):
        with faces_store.connection(self.path) as db:
            faces_store.sync_jobs(db, [('a', '/m/a.jpg', 'image', 's2', None, 'camera')], 'p1')
            self.assertFalse(faces_store.publish_faces(db, 'a', 's1', [self.face(0)]))
            self.assertEqual(db.execute('SELECT count(*) FROM faces').fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT state FROM face_jobs").fetchone()[0], 'queued')

    def test_failures_are_bounded_and_counted(self):
        with faces_store.connection(self.path) as db:
            faces_store.sync_jobs(db, [('a', '/m/a.jpg', 'image', 's', None, 'camera')], 'p1')
            for _ in range(faces_store.MAX_FACE_JOB_ATTEMPTS):
                self.assertIsNotNone(faces_store.next_job(db))
                faces_store.fail_job(db, 'a', 'decode failed')
            self.assertIsNone(faces_store.next_job(db))
            self.assertEqual(faces_store.counts(db), dict(remaining=1, completed=0, failed=1))

    def test_weak_frames_of_a_settled_track_stay_accepted(self):
        with faces_store.connection(self.path) as db:
            self.catalog = []
            self.publish(db, 'photo', [self.face(0)])
            confirmed_face = self.face(0, timestamp=0.5, track=0)
            weak = self.face(0, box=(0.1, 0.1, 0.3, 0.4), timestamp=1.5, track=0)
            # Make the second frame only moderately similar to the confirmed photo.
            import numpy as np
            mix = self.people[0] * 0.55 + self.people[1] * 0.83
            weak['embedding'] = mix / np.linalg.norm(mix)
            self.publish(db, 'v', [confirmed_face, weak], kind='video')
            rows = {(r['asset'], r['timestamp']): r['id'] for r in db.execute('SELECT id,asset,timestamp FROM faces')}
            box = (0.1, 0.1, 0.3, 0.4)
            labels = [('photo', None, *box, 'son', 'confirmed'), ('v', 0.5, *box, 'son', 'confirmed')]
            face_assignments.recompute(db, {'son': 'Our son'}, labels)
            assigned = {row['face']: row['source'] for row in db.execute('SELECT * FROM face_assignments')}
            self.assertEqual(assigned[rows[('v', 1.5)]], 'track')

    def test_version_one_index_migrates_in_place_and_regroups(self):
        import sqlite3
        self.path.parent.mkdir(parents=True)
        with contextlib.closing(sqlite3.connect(self.path)) as db, db:
            db.executescript("""CREATE TABLE faces_schema(version INTEGER NOT NULL); INSERT INTO faces_schema VALUES(1);
                CREATE TABLE face_state(id INTEGER PRIMARY KEY, change_token TEXT NOT NULL, paused INTEGER NOT NULL DEFAULT 0, grouped TEXT NOT NULL DEFAULT '');
                INSERT INTO face_state VALUES(1,'t',0,'0:0');
                CREATE TABLE face_jobs(asset TEXT PRIMARY KEY, path TEXT NOT NULL, kind TEXT NOT NULL, signature TEXT NOT NULL,
                    pipeline TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued', error TEXT, attempts INTEGER NOT NULL DEFAULT 0, capture_time REAL);
                INSERT INTO face_jobs(asset,path,kind,signature,pipeline,state) VALUES('a','/m/a.jpg','image','s','p1','complete');
                CREATE TABLE faces(id INTEGER PRIMARY KEY AUTOINCREMENT, asset TEXT NOT NULL);
                CREATE TABLE face_groups(face INTEGER PRIMARY KEY, grp INTEGER NOT NULL);""")
        with faces_store.connection(self.path) as db:
            self.assertEqual(db.execute('SELECT version FROM faces_schema').fetchone()[0], 3)
            self.assertIn('typical', {row[1] for row in db.execute('PRAGMA table_info(face_groups)')})
            self.assertEqual(db.execute("SELECT origin,state FROM face_jobs").fetchone()[:], ('other', 'complete'))
            self.assertTrue(faces_store.grouping_stale(db))

    def test_weak_embeddings_do_not_seed_groups_or_suggestions(self):
        with faces_store.connection(self.path) as db:
            self.catalog = []
            strong, weak = self.face(0), self.face(0, box=(0.5, 0.5, 0.7, 0.7))
            weak['norm'] = faces_store.MINIMUM_EMBEDDING_NORM - 1
            self.publish(db, 'a', [strong, weak])
            ids, matrix = faces_store.embeddings(db)
            self.assertEqual(len(ids), 1)
            self.assertEqual(matrix.shape, (1, DIMENSIONS))

    def test_labels_match_regenerated_faces_by_position_and_time(self):
        with faces_store.connection(self.path) as db:
            self.catalog = []
            self.publish(db, 'v', [self.face(0, box=(0.1, 0.1, 0.3, 0.3), timestamp=4.5)], kind='video')
            face = db.execute('SELECT id FROM faces').fetchone()[0]
            self.assertEqual(faces_store.match_label(db, 'v', 4.5, (0.11, 0.1, 0.31, 0.3)), face)
            self.assertIsNone(faces_store.match_label(db, 'v', 4.5, (0.5, 0.5, 0.7, 0.7)))
            self.assertIsNone(faces_store.match_label(db, 'v', 5.5, (0.1, 0.1, 0.3, 0.3)))
            self.assertIsNone(faces_store.match_label(db, 'v', None, (0.1, 0.1, 0.3, 0.3)))

    def test_recompute_confirms_suggests_propagates_tracks_and_groups_the_rest(self):
        with faces_store.connection(self.path) as db:
            self.catalog = []
            for asset in ('a1', 'a2', 'a3'):
                self.publish(db, asset, [self.face(0)])
            self.publish(db, 'b1', [self.face(1)]); self.publish(db, 'b2', [self.face(1)])
            self.publish(db, 'v', [self.face(0, timestamp=0.5, track=0), self.face(0, timestamp=1.5, track=0, representative=False),
                                   self.face(2, box=(0.6, 0.1, 0.8, 0.4), timestamp=0.5, track=1)], kind='video')
            ids = {row['asset'] + str(row['track']) + str(row['representative']): row['id'] for row in db.execute('SELECT * FROM faces')}
            box = (0.1, 0.1, 0.3, 0.4)
            labels = [('a1', None, *box, 'son', 'confirmed'), ('a3', None, *box, 'son', 'rejected'),
                      ('a2', None, *box, 'removed-person', 'confirmed')]
            self.assertTrue(faces_store.grouping_stale(db))
            face_assignments.recompute(db, {'son': 'Our son'}, labels)
            assigned = {row['face']: (row['person'], row['source']) for row in db.execute('SELECT * FROM face_assignments')}
            self.assertEqual(assigned, {ids['a101']: ('son', 'confirmed'), ids['a201']: ('son', 'suggested'),
                                        ids['v01']: ('son', 'suggested'), ids['v00']: ('son', 'track')})
            groups = dict(db.execute('SELECT face,grp FROM face_groups').fetchall())
            typical = dict(db.execute('SELECT face,typical FROM face_groups').fetchall())
            # One-face groups are their own average; pair members share one average.
            self.assertAlmostEqual(typical[ids['a301']], 1.0, places=5)
            self.assertAlmostEqual(typical[ids['b101']], typical[ids['b201']], places=5)
            self.assertLess(typical[ids['b101']], 1.0)
            self.assertEqual(groups, {ids['a301']: ids['a301'], ids['b101']: ids['b101'], ids['b201']: ids['b101'], ids['v11']: ids['v11']})
            self.assertFalse(faces_store.grouping_stale(db))


if __name__ == '__main__':
    unittest.main()
