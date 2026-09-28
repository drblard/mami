"""Full-scale filtered exact-reference checks for the supported MLX 4-bit path."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import time
from concurrent.futures import ThreadPoolExecutor
from common import save, percentiles

os.environ.setdefault('OPENBLAS_NUM_THREADS','4')


def main(args):
    import numpy as np
    import mlx.core as mx
    from vector_bench import base_data
    from vector_selection import top_indices
    mx.set_cache_limit(64*1024**2)
    vectors,metadata,files,cameras=base_data(args.run)
    full=np.memmap(args.run/'full.f32',dtype=np.float32,mode='r',shape=(len(vectors)*50,768))
    started=time.perf_counter()
    weights=mx.load(str(args.run/'mlx-50x-4bit.safetensors'))
    mx.eval(*weights.values())
    queries=np.load(args.run/'queries.npy')
    def score(query):
        result=mx.quantized_matmul(mx.array(query[None,:]),weights['weight'],weights['scales'],weights['biases'],transpose=True,group_size=64,bits=4)
        mx.eval(result)
        return np.array(result).ravel()
    score(queries[0])
    ready_seconds=time.perf_counter()-started
    camera=int(np.bincount(metadata['camera']).argmax())
    days=sorted(set(metadata['day'])-{0});day=int(days[len(days)//2])
    conditions=[(None,None),(f'camera = {camera}',np.tile(metadata['camera']==camera,50)),
                (f'day = {day}',np.tile(metadata['day']==day,50))]
    results=[]
    pool=ThreadPoolExecutor(max_workers=args.readers)
    for condition,mask in conditions:
        for i,query in enumerate(queries):
            started=time.perf_counter()
            scores=score(query)
            scored=time.perf_counter()
            if mask is not None:scores[~mask]=-np.inf
            ids=top_indices(scores,args.candidates)
            selected=time.perf_counter()
            ids.sort()
            if args.readers>1:
                chunks=np.array_split(ids,args.readers)
                selected_vectors=np.concatenate(list(pool.map(lambda chunk: full[chunk],chunks)))
            else:
                selected_vectors=full[ids]
            exact=selected_vectors@query
            reranked=time.perf_counter()
            ids=ids[np.argsort(-exact)]
            found=[];seen=set()
            for key in ids:
                asset=int(metadata['file_id'][key%len(vectors)])+int(key//len(vectors))*len(files)
                if asset not in seen:
                    seen.add(asset);found.append(asset)
                    if len(found)==60:break
            elapsed=(time.perf_counter()-started)*1000
            path=args.run/('exact-files-'+hashlib.sha256(f'50:{i}:{condition}'.encode()).hexdigest()[:16]+'.json')
            expected=set(json.loads(path.read_text()))
            results.append(dict(query=i,filter=condition,ms=elapsed,scan_ms=(scored-started)*1000,
                                select_ms=(selected-scored)*1000,rerank_ms=(reranked-selected)*1000,
                                recall=len(set(found)&expected)/len(expected)))
    summaries=[]
    for condition,_ in conditions:
        subset=[r for r in results if r['filter']==condition]
        summaries.append(dict(filter=condition,**percentiles([r['ms'] for r in subset]),
                              mean_recall=sum(r['recall'] for r in subset)/len(subset),minimum_recall=min(r['recall'] for r in subset)))
    result=dict(ready_seconds=ready_seconds,candidates=args.candidates,summaries=summaries,requests=results,
                parallel_readers=args.readers,
                active_MiB=mx.get_active_memory()/1024**2,cache_MiB=mx.get_cache_memory()/1024**2,
                caveat='Synthetic 50x, 24 text queries per scope, warmup included in readiness not query timings. Native text encoder excluded; no CapCut contention measurement.')
    save(args.run/f'quantized-quality-{time.time_ns()}.json',result)
    pool.shutdown()
    print(json.dumps({k:v for k,v in result.items() if k!='requests'},indent=2),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,required=True)
    parser.add_argument('--candidates',type=int,default=4096)
    parser.add_argument('--readers',type=int,default=1)
    main(parser.parse_args())
