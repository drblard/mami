"""Resumable LanceDB capacity/recall experiment; no model imports on search path."""
import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import resource
import shutil
import sqlite3
import time
from common import lock, progress, save, percentiles

os.environ.setdefault('RAYON_NUM_THREADS', '4')
os.environ.setdefault('OPENBLAS_NUM_THREADS', '4')
os.environ.setdefault('OMP_NUM_THREADS', '4')


def base_data(run):
    import numpy as np
    vectors = np.load(run / 'vectors.npy', mmap_mode='r')
    with sqlite3.connect(run / 'corpus.sqlite') as db:
        rows = db.execute('SELECT path,day,device,kind FROM frames ORDER BY id').fetchall()
    files = {path: i for i, path in enumerate(sorted({row[0] for row in rows}))}
    cameras = {name: i for i, name in enumerate(sorted({row[2] for row in rows}))}
    return vectors, dict(file_id=np.asarray([files[row[0]] for row in rows], dtype=np.int64),
                         day=np.asarray([int(row[1]) if row[1].isdigit() else 0 for row in rows], dtype=np.int32),
                         camera=np.asarray([cameras[row[2]] for row in rows], dtype=np.int16),
                         kind=np.asarray([row[3] == 'video' for row in rows], dtype=np.int8)), files, cameras


def connect(run, scale):
    import lancedb
    if (run/f'archived-lance-{scale}x.json').exists() and not (run/f'lance-{scale}x').exists():
        raise RuntimeError('This experiment table was archived; use cached references or restore its verified copy')
    session = lancedb.Session(index_cache_size_bytes=512*1024**2, metadata_cache_size_bytes=128*1024**2)
    return lancedb.connect(run / f'lance-{scale}x', session=session)


def build(args, run):
    import numpy as np
    import pyarrow as pa
    vectors, metadata, files, cameras = base_data(run)
    db = connect(run, args.scale)
    schema = pa.schema([('id', pa.int64()), ('file_id', pa.int64()), ('day', pa.int32()),
                        ('camera', pa.int16()), ('kind', pa.int8()), ('vector', pa.list_(pa.float32(), vectors.shape[1]))])
    if 'frames' in db.table_names():
        table = db.open_table('frames')
    else:
        table = db.create_table('frames', schema=schema)
    completed = table.count_rows()
    total = len(vectors)*args.scale
    # Each append is atomic; recover even if progress JSON lagged the commit.
    if completed % len(vectors) or completed > total:
        raise ValueError('Unexpected append boundary; inspect the retained table')
    task = f'vectors-{args.scale}x'
    started = time.monotonic()
    for replica in range(completed//len(vectors), args.scale):
        if shutil.disk_usage(run).free < 50*1024**3:
            raise RuntimeError('Less than 50 GiB free; stopped at a resumable boundary')
        if replica:
            rng = np.random.default_rng(20260928 + replica)
            expanded = np.asarray(vectors).copy()
            # Preserve real temporal clusters, but do not benchmark exact duplicates.
            for start in range(0, len(vectors), 4096):
                batch = expanded[start:start+4096]
                batch += rng.normal(0, .015, batch.shape).astype(np.float32)
                batch /= np.linalg.norm(batch, axis=1, keepdims=True)
        else:
            expanded = np.asarray(vectors)
        arrays = [pa.array(np.arange(len(vectors), dtype=np.int64)+replica*len(vectors)),
                  pa.array(metadata['file_id']+replica*len(files)),
                  pa.array(metadata['day']), pa.array(metadata['camera']), pa.array(metadata['kind']),
                  pa.FixedSizeListArray.from_arrays(pa.array(expanded.ravel()), vectors.shape[1])]
        table.add(pa.Table.from_arrays(arrays, schema=schema))
        progress(run, task, stage='append', done=(replica+1)*len(vectors), total=total,
                 seconds=round(time.monotonic()-started,2))
    index_name = 'vector_idx'
    if index_name not in {index.name for index in table.list_indices()}:
        progress(run, task, stage='index_training', rows=total, partitions=args.partitions,
                 sub_vectors=args.sub_vectors, max_iterations=20, sample_rate=64)
        table.create_index(metric='cosine', index_type=args.index_type, num_partitions=args.partitions,
                           num_sub_vectors=args.sub_vectors, num_bits=args.bits, max_iterations=20, sample_rate=64, name=index_name)
    for column in ('file_id', 'day', 'camera'):
        if column + '_idx' not in {index.name for index in table.list_indices()}:
            table.create_scalar_index(column, index_type='BTREE', name=column + '_idx')
    result = dict(rows=table.count_rows(), seconds=round(time.monotonic()-started,2),
                  index_stats=asdict(table.index_stats(index_name)), cameras=cameras, files=len(files)*args.scale,
                  peak_RSS_MiB=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024**2,
                  storage_bytes=sum(path.stat().st_size for path in (run / f'lance-{args.scale}x').rglob('*') if path.is_file()),
                  index_type=args.index_type, partitions=args.partitions, sub_vectors=args.sub_vectors)
    save(run / f'vectors-{args.scale}x-build.json', result)
    progress(run, task, stage='complete', **result)


def unique_files(rows, limit=60):
    found = []
    seen = set()
    for row in rows:
        if row['file_id'] not in seen:
            seen.add(row['file_id'])
            found.append(row['file_id'])
            if len(found) == limit:
                break
    return found


def benchmark(args, run):
    import numpy as np
    vectors, metadata, files, cameras = base_data(run)
    queries = np.load(run / 'queries.npy')
    query_names = json.loads((run / 'queries.json').read_text())
    started = time.perf_counter()
    db = connect(run, args.scale)
    table = db.open_table('frames')
    open_ms = (time.perf_counter()-started)*1000
    camera = int(np.bincount(metadata['camera']).argmax())
    days = sorted(set(metadata['day']) - {0})
    day = int(days[len(days)//2])
    conditions = [(None, np.ones(len(vectors), dtype=bool)),
                  (f'camera = {camera}', metadata['camera'] == camera),
                  (f'day = {day}', metadata['day'] == day)]
    exact = {}
    if args.scale == 1:
        for condition, mask in conditions:
            for i, query in enumerate(queries):
                scores = vectors @ query
                scores[~mask] = -np.inf
                best = np.full(len(files), -np.inf, dtype=np.float32)
                np.maximum.at(best, metadata['file_id'], scores)
                order = np.argsort(-best, kind='stable')
                exact[condition, i] = set(int(v) for v in order[np.isfinite(best[order])][:60])
    results = []
    for probes in args.probes:
        for condition, mask in conditions:
            for i, query in enumerate(queries):
                started = time.perf_counter()
                search = (table.search(query).metric('cosine').nprobes(probes)
                          .refine_factor(args.refine).limit(args.candidates).select(['id', 'file_id', '_distance']))
                if args.adaptive:
                    search = search.minimum_nprobes(probes).maximum_nprobes(args.partitions)
                if condition:
                    search = search.where(condition, prefilter=True)
                    if args.exact_narrow and int(mask.sum())*args.scale <= 50000:
                        search = search.bypass_vector_index()
                hits = search.to_list()
                found = unique_files(hits)
                elapsed = (time.perf_counter()-started)*1000
                expected = exact.get((condition, i))
                row = dict(query=query_names[i], filter=condition, probes=probes, ms=round(elapsed,3),
                           distinct_files=len(found), recall60=len(set(found)&expected)/len(expected) if expected else None)
                results.append(row)
    summaries = []
    for probes in args.probes:
        for condition, _ in conditions:
            subset = [row for row in results if row['probes'] == probes and row['filter'] == condition]
            recalls = [row['recall60'] for row in subset if row['recall60'] is not None]
            summaries.append(dict(probes=probes, filter=condition, **percentiles([row['ms'] for row in subset]),
                                  mean_recall60=sum(recalls)/len(recalls) if recalls else None,
                                  min_recall60=min(recalls) if recalls else None))
    output = run / f'vectors-{args.scale}x-benchmark-{time.time_ns()}.json'
    result = dict(open_ms=open_ms, candidates=args.candidates, refine=args.refine, summaries=summaries,
                  adaptive=args.adaptive, exact_narrow=args.exact_narrow,
                  peak_RSS_MiB=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024**2, requests=results,
                  caveat='Process-open/warm OS-cache timings; synthetic 50x is capacity, not semantic relevance.')
    save(output, result)
    print(json.dumps({k:v for k,v in result.items() if k != 'requests'},indent=2),flush=True)
    print(output,flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--scale', type=int, default=1)
    parser.add_argument('--build', action='store_true')
    parser.add_argument('--index-type', default='IVF_PQ')
    parser.add_argument('--partitions', type=int, default=256)
    parser.add_argument('--sub-vectors', type=int, default=96)
    parser.add_argument('--bits', type=int, default=8)
    parser.add_argument('--probes', type=int, nargs='+', default=[8, 16, 32])
    parser.add_argument('--candidates', type=int, default=1024)
    parser.add_argument('--refine', type=int, default=2)
    parser.add_argument('--adaptive', action='store_true')
    parser.add_argument('--exact-narrow', action='store_true')
    args = parser.parse_args()
    with lock(args.run, f'vectors-{args.scale}x') as run:
        if args.build:
            build(args, run)
        else:
            benchmark(args, run)
