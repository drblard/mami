"""Checkpoint exact-versus-ANN file recall at full scale, including difficult queries."""
import argparse
import json
import os
from pathlib import Path
import time
from common import lock, progress, save

os.environ.setdefault('RAYON_NUM_THREADS','4')
os.environ.setdefault('OPENBLAS_NUM_THREADS','4')


def main(args):
    import lancedb
    import numpy as np
    from vector_bench import unique_files
    with lock(args.run,f'quality-{args.scale}x') as run:
        queries=np.load(run/'queries.npy')
        names=json.loads((run/'queries.json').read_text())
        session=lancedb.Session(index_cache_size_bytes=512*1024**2,metadata_cache_size_bytes=128*1024**2)
        table=lancedb.connect(run/f'lance-{args.scale}x',session=session).open_table('frames')
        output=run/f'quality-{args.scale}x-v{table.version}.json'
        results=json.loads(output.read_text()) if output.exists() else []
        selected=[0,1,2,7,10,11,14,20]
        for i in selected:
            if any(row['query']==names[i] for row in results):continue
            started=time.perf_counter()
            exact_rows=table.search(queries[i]).metric('cosine').bypass_vector_index().limit(4096).select(['id','file_id','_distance']).to_list()
            expected=set(unique_files(exact_rows))
            exact_seconds=time.perf_counter()-started
            measurements=[]
            for probes in (64,128,256,512):
                started=time.perf_counter()
                rows=table.search(queries[i]).metric('cosine').nprobes(probes).refine_factor(1).limit(1024).select(['id','file_id','_distance']).to_list()
                found=set(unique_files(rows))
                measurements.append(dict(probes=probes,ms=(time.perf_counter()-started)*1000,
                                         recall60=len(expected&found)/len(expected),files=len(found)))
            row=dict(query=names[i],exact_seconds=exact_seconds,exact_files=len(expected),measurements=measurements)
            results.append(row);save(output,results)
            progress(run,f'quality-{args.scale}x',stage='query',done=len(results),total=len(selected),**row)
        progress(run,f'quality-{args.scale}x',stage='complete',output=str(output))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,required=True)
    parser.add_argument('--scale',type=int,default=50)
    main(parser.parse_args())
