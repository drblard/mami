"""Immutable packed vector generations; queries never open per-frame .npy files.

Build into a NEW directory, then publish manifest.json last. Full-precision rows
stay memory mapped for exact reranking. This is the base-generation component;
incremental overlay/compaction must be integrated before making it the default.
"""
import contextlib
import fcntl
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
import json
import os
from pathlib import Path
import sqlite3
import time

from model_config import VISUAL_MODEL
from vector_selection import top_indices

DIMENSIONS = 768
FORMAT_VERSION = 3
QUANTIZATION_BITS = 4
QUANTIZATION_GROUP_SIZE = 64
VECTOR_CACHE_BYTES = 64 * 1024 * 1024
RERANK_CANDIDATES = 1024
FILTERED_RERANK_CANDIDATES = 4096
NARROW_SCOPE_ROWS = 50000
READ_THREADS = 8
BUILD_BATCH_ROWS = 4096


def build(projection, destination, owner=None, checkpoint=lambda: None):
    import numpy as np
    import mlx.core as mx
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    mx.set_cache_limit(VECTOR_CACHE_BYTES)
    with contextlib.closing(sqlite3.connect(Path(projection).resolve().as_uri()+'?mode=ro',uri=True)) as source:
        source.execute('BEGIN')
        count = source.execute('SELECT count(*) FROM embeddings').fetchone()[0]
        if count == 0:
            raise ValueError('No visual embeddings are ready')
        source_checkpoint = source.execute('SELECT identity,sequence,epoch,anchor FROM checkpoint WHERE id=1 AND ready=1').fetchone()
        if source_checkpoint is None:
            raise ValueError('Search projection is not ready')
        projection_sequence = (source.execute('SELECT coalesce(max(sequence),0) FROM vector_events').fetchone()[0]
                               if source.execute("SELECT 1 FROM sqlite_master WHERE name='vector_events'").fetchone() else 0)
        full = np.lib.format.open_memmap(destination/'vectors.npy',mode='w+',dtype=np.float32,shape=(count,DIMENSIONS))
        packed = np.empty((count,DIMENSIONS*QUANTIZATION_BITS//32),dtype=np.uint32)
        scales = np.empty((count,DIMENSIONS//QUANTIZATION_GROUP_SIZE),dtype=np.float32)
        biases = np.empty_like(scales)
        with contextlib.closing(sqlite3.connect(destination/'rows.sqlite')) as rows:
            rows.executescript('CREATE TABLE files(id INTEGER PRIMARY KEY,asset TEXT UNIQUE,path TEXT,kind TEXT,camera TEXT,captured TEXT,shape TEXT,arrival INTEGER);'
                              'CREATE INDEX file_camera ON files(camera); CREATE INDEX file_date ON files(captured);'
                              'CREATE TABLE vectors(id INTEGER PRIMARY KEY,file_id INTEGER,ordinal INTEGER,frame TEXT,timestamp REAL);')
            file_ids = {}
            columns={row[1] for row in source.execute('PRAGMA table_info(files)')}
            shape_column='shape' if 'shape' in columns else "'unknown'"
            arrival_column='arrival' if 'arrival' in columns else '0'
            for asset,path,kind,camera,captured,shape,arrival in source.execute(f'SELECT asset,path,kind,camera,captured,{shape_column},{arrival_column} FROM files ORDER BY asset'):
                file_id=len(file_ids);file_ids[asset]=file_id
                rows.execute('INSERT INTO files VALUES(?,?,?,?,?,?,?,?)',(file_id,asset,path,kind,camera,captured,shape,arrival))
            row_files=np.empty(count,dtype=np.int32)
            mapped = {}
            cursor=source.execute('SELECT asset,ordinal,vector_path,vector_row,frame,timestamp FROM embeddings ORDER BY asset,ordinal')
            offset=0
            while True:
                checkpoint()
                batch=cursor.fetchmany(BUILD_BATCH_ROWS)
                if not batch:break
                for i,(asset,ordinal,path,matrix_row,frame,timestamp) in enumerate(batch,start=offset):
                    if matrix_row is None:
                        vector=np.load(path,allow_pickle=False)
                    else:
                        if path not in mapped:mapped[path]=np.load(path,mmap_mode='r',allow_pickle=False)
                        vector=mapped[path][matrix_row]
                    if vector.shape!=(DIMENSIONS,) or not np.isfinite(vector).all():
                        raise ValueError('Invalid source embedding: '+path)
                    full[i]=vector
                    row_files[i]=file_ids[asset]
                    rows.execute('INSERT INTO vectors VALUES(?,?,?,?,?)',(i,file_ids[asset],ordinal,frame,timestamp))
                q,s,b=mx.quantize(mx.array(full[offset:offset+len(batch)]),group_size=QUANTIZATION_GROUP_SIZE,bits=QUANTIZATION_BITS)
                mx.eval(q,s,b)
                packed[offset:offset+len(batch)]=np.array(q)
                scales[offset:offset+len(batch)]=np.array(s)
                biases[offset:offset+len(batch)]=np.array(b)
                offset+=len(batch)
            rows.commit()
            if rows.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                raise ValueError('Packed row map failed integrity check')
        full.flush()
        checkpoint()
        np.save(destination/'file_ids.npy',row_files)
        mx.save_safetensors(str(destination/'quantized.safetensors'),dict(weight=mx.array(packed),scales=mx.array(scales),biases=mx.array(biases)))
        (destination/'.readers').touch(exist_ok=False)
        manifest=dict(version=FORMAT_VERSION,model=VISUAL_MODEL,dimensions=DIMENSIONS,rows=count,bits=QUANTIZATION_BITS,
                      group_size=QUANTIZATION_GROUP_SIZE,source_identity=source_checkpoint[0],sequence=source_checkpoint[1],
                      epoch=source_checkpoint[2],anchor=source_checkpoint[3],projection_sequence=projection_sequence,owner=owner,created=time.time())
        for path in destination.iterdir():
            if path.is_file():
                with path.open('rb') as stream:os.fsync(stream.fileno())
        with (destination/'manifest.json').open('x') as output:
            json.dump(manifest,output,indent=2);output.flush();os.fsync(output.fileno())
        descriptor=os.open(destination,os.O_RDONLY)
        try:os.fsync(descriptor)
        finally:os.close(descriptor)
        return manifest


class PackedIndex:
    def __init__(self,directory):
        import numpy as np
        import mlx.core as mx
        self.np,self.mx=np,mx
        directory=Path(directory)
        self.manifest=json.loads((directory/'manifest.json').read_text())
        if self.manifest['version']!=FORMAT_VERSION or tuple(self.manifest['model'])!=VISUAL_MODEL:
            raise ValueError('Packed index version/model mismatch')
        self.lease=(directory/'.readers').open('rb')
        fcntl.flock(self.lease,fcntl.LOCK_SH)
        mx.set_cache_limit(VECTOR_CACHE_BYTES)
        self.weights=mx.load(str(directory/'quantized.safetensors'))
        self.full=np.load(directory/'vectors.npy',mmap_mode='r',allow_pickle=False)
        self.file_ids=np.load(directory/'file_ids.npy',mmap_mode='r',allow_pickle=False)
        if self.full.shape!=(self.manifest['rows'],DIMENSIONS) or len(self.file_ids)!=len(self.full):
            raise ValueError('Packed index row counts differ')
        self.rows=sqlite3.connect((directory/'rows.sqlite').resolve().as_uri()+'?mode=ro&immutable=1',uri=True,check_same_thread=False)
        self.rows.row_factory=sqlite3.Row
        self.pool=ThreadPoolExecutor(max_workers=READ_THREADS)

    @lru_cache(maxsize=8)
    def scope_mask(self,camera,from_date,through_date,paths,kind,shape,assets,arrival_through):
        clauses=[];params=[]
        for column,value in [('camera',camera),('kind',kind),('shape',shape)]:
            if value is not None:clauses.append(column+'=?');params.append(value)
        if from_date is not None:clauses.append('captured>=?');params.append(from_date)
        if through_date is not None:clauses.append("captured<=? AND captured!=''");params.append(through_date)
        if arrival_through is not None:clauses.append('arrival<=?');params.append(arrival_through)
        sql='SELECT id,path,asset FROM files'+(' WHERE '+' AND '.join(clauses) if clauses else '')
        ids=[row['id'] for row in self.rows.execute(sql,params) if (paths is None or row['path'] in paths) and (assets is None or row['asset'] in assets)]
        return self.np.isin(self.file_ids,ids)

    def search(self,query,*,camera=None,from_date=None,through_date=None,paths=None,kind=None,shape=None,
               assets=None,arrival_through=None,limit=60,exclude_assets=()):
        np,mx=self.np,self.mx
        if not 1 <= limit <= 60:
            raise ValueError('Result limit must be between 1 and 60')
        vector=np.asarray(query,dtype=np.float32)
        if vector.shape!=(DIMENSIONS,) or not np.isfinite(vector).all():
            raise ValueError('Invalid query embedding')
        output=mx.quantized_matmul(mx.array(vector[None,:]),self.weights['weight'],self.weights['scales'],self.weights['biases'],
                                   transpose=True,group_size=QUANTIZATION_GROUP_SIZE,bits=QUANTIZATION_BITS)
        mx.eval(output)
        scores=np.array(output).ravel()
        if exclude_assets:
            excluded = [row[0] for asset in exclude_assets for row in self.rows.execute('SELECT id FROM files WHERE asset=?',(asset,))]
            scores[np.isin(self.file_ids,excluded)]=-np.inf
        scoped=any(value is not None for value in (camera,from_date,through_date,paths,kind,shape,assets,arrival_through))
        if scoped:
            mask=self.scope_mask(camera,from_date,through_date,None if paths is None else frozenset(paths),kind,shape,
                                 None if assets is None else frozenset(assets),arrival_through)
            scores[~mask]=-np.inf
        candidate_count = (FILTERED_RERANK_CANDIDATES
                           if scoped and np.count_nonzero(np.isfinite(scores)) <= NARROW_SCOPE_ROWS
                           else RERANK_CANDIDATES)
        candidates=top_indices(scores,candidate_count)
        candidates.sort()
        if not len(candidates):return []
        chunks=np.array_split(candidates,min(READ_THREADS,len(candidates)))
        values=np.concatenate(list(self.pool.map(lambda chunk:self.full[chunk],chunks)))
        exact=values@vector
        order=np.argsort(-exact)
        hits,seen=[],set()
        for rank in order:
            row_id=int(candidates[rank]);file_id=int(self.file_ids[row_id])
            if file_id in seen:continue
            seen.add(file_id)
            row=self.rows.execute('SELECT f.asset,f.path,f.kind,v.frame,v.timestamp FROM vectors v JOIN files f ON f.id=v.file_id WHERE v.id=?',(row_id,)).fetchone()
            hits.append(dict(row,score=float(exact[rank])))
            if len(hits)==limit:break
        return hits

    def close(self):
        self.pool.shutdown()
        self.rows.close()
        self.weights=None
        self.scope_mask.cache_clear()
        self.full=None
        self.file_ids=None
        self.mx.clear_cache()
        self.lease.close()
