"""Compare real search-process results to full-precision, distinct-file references."""
import argparse
import contextlib
import json
import os
from pathlib import Path
import select
import sqlite3
import subprocess
import sys
import time

os.environ.setdefault('OPENBLAS_NUM_THREADS','4')


def main(args):
    import numpy as np
    args.output.mkdir(parents=True,exist_ok=False)
    sys.path.insert(0,str(args.bundle/'Contents/Resources'))
    from text_encoder import NativeEncoder
    from vector_sync import resolve_generation
    generation=resolve_generation(args.vectors)
    names=json.loads(args.queries.read_text())
    encoder=NativeEncoder(args.bundle/'Contents/MacOS/Mami',args.encoder)
    try:features=np.stack([encoder.encode(name) for name in names])
    finally:encoder.close()
    with contextlib.closing(sqlite3.connect((generation/'rows.sqlite').as_uri()+'?mode=ro',uri=True)) as db:
        files=db.execute('SELECT id,asset,camera,captured,kind,shape FROM files ORDER BY id').fetchall()
    full=np.load(generation/'vectors.npy',mmap_mode='r',allow_pickle=False)
    ids=np.load(generation/'file_ids.npy',mmap_mode='r',allow_pickle=False)
    scores=np.full((len(files),len(names)),-np.inf,dtype=np.float32)
    started=time.monotonic()
    for offset in range(0,len(full),16384):
        block=full[offset:offset+16384]@features.T
        np.maximum.at(scores,ids[offset:offset+len(block)],block)
    exact_seconds=time.monotonic()-started
    np.save(args.output/'exact-file-scores.npy',scores)
    del full,ids,features
    def most(column):
        from collections import Counter
        return Counter(row[column] for row in files if row[column]).most_common(1)[0][0]
    days=sorted({row[3][:8] for row in files if row[3]})
    day=days[len(days)//2]
    favorites=[row[1] for row in files[::max(1,len(files)//120)]][:120]
    scopes=[('all',{}),('camera',dict(camera=most(2))),('day',{'from':day,'through':day+'235959'}),
            ('shape',dict(shape=most(5))),('kind',dict(kind=most(4))),('favorite-style',dict(assets=favorites))]
    def accepts(row,scope):
        return ((scope.get('camera') is None or row[2]==scope['camera']) and
                (scope.get('from') is None or row[3]>=scope['from']) and
                (scope.get('through') is None or (row[3] and row[3]<=scope['through'])) and
                (scope.get('kind') is None or row[4]==scope['kind']) and
                (scope.get('shape') is None or row[5]==scope['shape']) and
                (scope.get('assets') is None or row[1] in scope['assets']))
    command=[sys.executable,'-B',str(args.bundle/'Contents/Resources/search_worker.py'),'--index',str(args.index),
             '--packed-index',str(args.vectors),'--projection',str(args.projection),'--native-encoder',str(args.encoder)]
    results=[]
    with (args.output/'worker.log').open('x') as log:
        process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=log,text=True)
        def read():
            if not select.select([process.stdout],[],[],40)[0]:raise TimeoutError('Search acceptance deadline')
            reply=json.loads(process.stdout.readline())
            if reply.get('error'):raise RuntimeError(reply['error'])
            return reply
        def query(name,scope):
            process.stdin.write(json.dumps(dict(query=name,scope=scope,mode='visual'))+'\n');process.stdin.flush()
            return read()
        try:
            read()
            deadline=time.monotonic()+40
            while query('a photo',{}).get('visual_pending'):
                if time.monotonic()>deadline:raise TimeoutError('Visual warmup deadline')
                time.sleep(.05)
            for label,scope in scopes:
                allowed=np.array([row[0] for row in files if accepts(row,scope)],dtype=np.int64)
                allowed_assets={files[i][1] for i in allowed}
                for i,name in enumerate(names):
                    relevant=allowed[np.isfinite(scores[allowed,i])]
                    expected=relevant[np.argsort(-scores[relevant,i],kind='stable')[:60]]
                    expected_assets={files[file_id][1] for file_id in expected}
                    tick=time.monotonic();reply=query(name,scope);elapsed=(time.monotonic()-tick)*1000
                    found={hit['asset'] for hit in reply['hits']}
                    if not found<=allowed_assets:raise ValueError('Search returned an out-of-scope asset')
                    result=dict(scope=label,query=name,ms=elapsed,hits=len(found),expected=len(expected_assets),
                                recall=len(found&expected_assets)/len(expected_assets) if len(expected_assets) else 1)
                    results.append(result)
                subset=results[-len(names):]
                print(json.dumps(dict(scope=label,minimum_recall=min(row['recall'] for row in subset),
                                      p95_ms=float(np.percentile([row['ms'] for row in subset],95)))),flush=True)
                (args.output/'progress.json').write_text(json.dumps(results,indent=2))
        finally:
            process.stdin.close()
            try:process.wait(timeout=20)
            except subprocess.TimeoutExpired:process.kill();process.wait()
    summary=dict(rows=len(files),queries=len(names),exact_reference_seconds=exact_seconds,
                 minimum_recall=min(row['recall'] for row in results),
                 p95_ms=float(np.percentile([row['ms'] for row in results],95)),requests=results,
                 caveat='Full-precision per-file maxima; fresh search process after reference scan, warm OS caches. Favorite scope uses explicit fixture IDs; no personal favorites are modified.')
    (args.output/'result.json').write_text(json.dumps(summary,indent=2))
    print(json.dumps({key:value for key,value in summary.items() if key!='requests'},indent=2),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('bundle','index','vectors','projection','encoder','queries','output'):
        parser.add_argument('--'+name,type=Path,required=True)
    main(parser.parse_args())
