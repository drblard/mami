"""Consolidate a consistent live-catalog snapshot into resumable benchmark inputs."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import time
from urllib.parse import unquote, urlparse
from common import lock, progress, save


def device_of(value):
    """Match the native Media.device rules, including unknown iCloud cameras."""
    info = value.get('metadata') or {}
    parts = Path(unquote(urlparse(value.get('url', '')).path)).parts
    if info.get('source') != 'iCloud' and 'Originals' in parts:
        i = len(parts)-1-list(reversed(parts)).index('Originals')
        if i+1 < len(parts) and parts[i+1] not in ('iCloud','iCloud-Photos'):
            return parts[i+1]
    if info.get('camera'):
        return info['camera']
    if info.get('camera') is None and len(info.get('details', [])) > 1:
        return info['details'][-1]
    if info.get('source') == 'iCloud' or any(p in parts for p in ('iCloud','iCloud-Photos')):
        return 'iCloud · Device unavailable'
    return 'Unknown device'


def prepare(args):
    import numpy as np
    with lock(args.run, 'corpus') as run:
        inputs = dict(catalog=str(args.catalog.resolve()), index=str(args.index.resolve()),
                      speech=str(args.speech.resolve()) if args.speech else None, pipeline=args.pipeline)
        contract = run / 'corpus-inputs.json'
        if contract.exists() and json.loads(contract.read_text()) != inputs:
            raise ValueError('Input paths or pipeline changed; use a new run')
        if not contract.exists():
            save(contract, inputs)
        if (run / 'corpus.json').exists():
            print((run / 'corpus.json').read_text())
            return
        snapshot = run / 'source.sqlite'
        if not snapshot.exists():
            progress(run, 'corpus', stage='snapshot')
            with sqlite3.connect(args.catalog.resolve().as_uri() + '?mode=ro', uri=True, timeout=30) as source:
                with sqlite3.connect(str(snapshot) + '.tmp') as target:
                    source.backup(target, pages=4096)
            os.replace(str(snapshot) + '.tmp', snapshot)
        db = sqlite3.connect(snapshot.resolve().as_uri() + '?mode=ro', uri=True)
        manifest = json.loads((args.index / 'samples.json').read_text())
        base = manifest['samples']
        base_vectors = np.load(args.index / 'embeddings.npy', mmap_mode='r')
        metadata = {}
        for asset, payload in db.execute('SELECT asset,payload FROM media'):
            value = json.loads(payload)
            info = value.get('metadata') or {}
            metadata[value['path']] = dict(asset=asset, day=str(info.get('sortDate', ''))[:8],
                                           device=device_of(value), kind=value['kind'])
        sql = "SELECT payload FROM index_units WHERE stage='embedding' AND pipeline=? AND asset IN (SELECT asset FROM media) ORDER BY asset,ordinal"
        count = len(base) + db.execute('SELECT count(*) FROM (' + sql + ')', (args.pipeline,)).fetchone()[0]
        vector_file = run / 'vectors.npy'
        if vector_file.exists():
            vectors = np.lib.format.open_memmap(vector_file, mode='r+')
            if vectors.shape != (count, base_vectors.shape[1]):
                raise ValueError('Existing corpus shape differs; use a new run')
        else:
            vectors = np.lib.format.open_memmap(vector_file, mode='w+', dtype='float32', shape=(count, base_vectors.shape[1]))
        out = sqlite3.connect(run / 'corpus.sqlite')
        out.execute('PRAGMA synchronous=FULL')
        out.executescript('CREATE TABLE IF NOT EXISTS frames(id INTEGER PRIMARY KEY, asset TEXT, path TEXT, timestamp REAL, frame TEXT, day TEXT, device TEXT, kind TEXT);'
                          'CREATE TABLE IF NOT EXISTS segments(id INTEGER PRIMARY KEY,path TEXT,start REAL,text TEXT);')
        cursor = out.execute('SELECT coalesce(max(id)+1,0) FROM frames').fetchone()[0]
        started = time.monotonic()
        def records():
            for i, sample in enumerate(base):
                yield sample, None, i
            for row in db.execute(sql, (args.pipeline,)):
                value = json.loads(row[0])
                yield value['sample'], value['vector'], None
        pending = []
        for i, (sample, vector_path, base_index) in enumerate(records()):
            if i < cursor:
                continue
            vectors[i] = np.load(vector_path, allow_pickle=False) if vector_path else base_vectors[base_index]
            info = metadata.get(sample['path'], dict(asset=sample['path'], day='', device='Unknown', kind=sample['kind']))
            pending.append((i, info['asset'], sample['path'], sample.get('timestamp'), sample['frame'], info['day'], info['device'], info['kind']))
            if len(pending) >= 4096 or i == count-1:
                # Vector data reaches stable storage before its row checkpoint.
                vectors.flush()
                with vector_file.open('rb') as handle:
                    os.fsync(handle.fileno())
                out.executemany('INSERT INTO frames VALUES(?,?,?,?,?,?,?,?)', pending)
                out.commit()
                progress(run, 'corpus', stage='vectors', done=i+1, total=count, seconds=round(time.monotonic()-started,2))
                pending.clear()
        with out:
            out.execute('DELETE FROM segments')
            for file in sorted(args.speech.glob('*.json')) if args.speech else []:
                if file.stem.isdigit():
                    item = json.loads(file.read_text())
                    out.executemany('INSERT INTO segments(path,start,text) VALUES(?,?,?)',
                                    [(item['path'], s['start'], s['text']) for s in item.get('transcript', {}).get('segments', [])])
            for payload, in db.execute("SELECT payload FROM index_units WHERE stage='speech' AND pipeline=? AND asset IN (SELECT asset FROM media)", (args.pipeline,)):
                value = json.loads(payload)
                out.executemany('INSERT INTO segments(path,start,text) VALUES(?,?,?)',
                                [(value['path'], s['start'], s['text']) for s in value['segments']])
            out.execute('CREATE INDEX IF NOT EXISTS frames_path ON frames(path,id)')
            out.execute('CREATE INDEX IF NOT EXISTS frames_day ON frames(day,id)')
        result = dict(rows=count, dimensions=vectors.shape[1], files=out.execute('SELECT count(DISTINCT path) FROM frames').fetchone()[0],
                      segments=out.execute('SELECT count(*) FROM segments').fetchone()[0],
                      source_catalog=str(args.catalog), base_index=str(args.index),
                      source_token=db.execute('SELECT change_token FROM state').fetchone()[0],
                      vector_bytes=vectors.nbytes, created=time.time())
        save(run / 'corpus.json', result)
        progress(run, 'corpus', stage='complete', **result)
        db.close()
        out.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--catalog', type=Path, required=True)
    parser.add_argument('--index', type=Path, required=True)
    parser.add_argument('--speech', type=Path)
    parser.add_argument('--pipeline', default='siglip2-75de2d55-whisper-a4aaeec0-1s-ro-chunk30-v1')
    prepare(parser.parse_args())
