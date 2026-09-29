import contextlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

try:
    import numpy as np
    import mlx.core as mx
except ImportError:
    np = mx = None

from packed_vectors import PackedIndex, build


@unittest.skipIf(mx is None, 'MLX/NumPy checks run on the target Mac')
class PackedVectorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.projection = self.root/'projection.sqlite'
        rng = np.random.default_rng(42)
        self.vectors = rng.normal(size=(90,768)).astype(np.float32)
        self.vectors /= np.linalg.norm(self.vectors,axis=1,keepdims=True)
        matrix = self.root/'source.npy'
        np.save(matrix,self.vectors)
        with contextlib.closing(sqlite3.connect(self.projection)) as db, db:
            db.executescript('CREATE TABLE checkpoint(id INTEGER,identity TEXT,sequence INTEGER,epoch TEXT,anchor TEXT,ready INTEGER);'
                             'INSERT INTO checkpoint VALUES(1,"fixture",0,"epoch",NULL,1);'
                             'CREATE TABLE files(asset TEXT,path TEXT,kind TEXT,camera TEXT,captured TEXT);'
                             'CREATE TABLE embeddings(asset TEXT,ordinal INTEGER,vector_path TEXT,vector_row INTEGER,frame TEXT,timestamp REAL);')
            for i in range(30):
                asset=f'asset-{i:03d}'
                db.execute('INSERT INTO files VALUES(?,?,?,?,?)',(asset,asset,'video','A' if i<15 else 'B','20260928' if i<15 else '20250928'))
                for j in range(3):
                    db.execute('INSERT INTO embeddings VALUES(?,?,?,?,?,?)',(asset,j,str(matrix),i*3+j,f'/offline/{i}-{j}.jpg',j+.5))
        self.destination=self.root/'packed'
        build(self.projection,self.destination)
        self.index=PackedIndex(self.destination)
        self.addCleanup(self.index.close)

    def test_results_match_exact_file_maxima_and_do_not_require_source_artifacts(self):
        (self.root/'source.npy').unlink()
        query=self.vectors[23]
        scores=(self.vectors@query).reshape(30,3).max(axis=1)
        expected=[f'asset-{i:03d}' for i in np.argsort(-scores)]
        hits=self.index.search(query)
        self.assertEqual([hit['asset'] for hit in hits],expected)
        self.assertEqual(hits[0]['timestamp'],2.5)

    def test_filters_apply_before_limit(self):
        for kwargs in (dict(camera='B'),dict(through_date='20251231')):
            hits=self.index.search(self.vectors[0],limit=5,**kwargs)
            self.assertEqual(len(hits),5)
            self.assertTrue(all(int(hit['asset'].split('-')[1])>=15 for hit in hits))
        self.assertEqual(self.index.search(self.vectors[0],paths=[]),[])
        self.assertEqual([hit['asset'] for hit in self.index.search(self.vectors[0],paths=['asset-029'])],['asset-029'])
        self.assertEqual(self.index.search(self.vectors[0],paths=['asset-029'],assets=['asset-001']),[])
        self.assertEqual([hit['asset'] for hit in self.index.search(self.vectors[0],assets=['asset-029'])],['asset-029'])
        self.assertEqual(self.index.search(self.vectors[0],camera='A',assets=['asset-029']),[])

    def test_small_scope_is_exact_even_when_frame_budget_would_hide_videos(self):
        query=self.vectors[0]
        scores=(self.vectors@query).reshape(30,3).max(axis=1)
        expected=[f'asset-{i:03d}' for i in (15+np.argsort(-scores[15:]))[:5]]
        with patch('packed_vectors.FILTERED_RERANK_CANDIDATES',1):
            self.assertEqual([hit['asset'] for hit in self.index.search(query,camera='B',limit=5)],expected)

    def test_incomplete_generation_and_overwrite_are_rejected(self):
        with self.assertRaises(FileExistsError):
            build(self.projection,self.destination)
        incomplete=self.root/'incomplete';incomplete.mkdir()
        with self.assertRaises(FileNotFoundError):
            PackedIndex(incomplete)

    def test_empty_generation_can_replace_a_deleted_library(self):
        with contextlib.closing(sqlite3.connect(self.projection)) as db,db:
            db.execute('DELETE FROM embeddings')
            db.execute('DELETE FROM files')
        destination=self.root/'empty'
        build(self.projection,destination)
        empty=PackedIndex(destination)
        try:
            self.assertEqual(empty.manifest['rows'],0)
            self.assertEqual(empty.search(self.vectors[0]),[])
        finally:empty.close()


if __name__=='__main__':unittest.main()
