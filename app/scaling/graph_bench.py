"""Compare a memory-mapped HNSW candidate against IVF for difficult text queries."""
import argparse
import json
import os
from pathlib import Path
import resource
import shutil
import time
from common import lock, progress, save, percentiles

os.environ.setdefault('OPENBLAS_NUM_THREADS','4')


def main(args):
    import numpy as np
    from usearch.index import Index
    from vector_bench import base_data
    task=f'graph-{args.scale}x'+('' if args.dtype=='i8' else '-'+args.dtype)
    with lock(args.run,task) as run:
        vectors,metadata,files,cameras=base_data(run)
        path=run/(task+'.usearch')
        if not path.exists() and (run/('archived-'+path.name+'.json')).exists():
            raise RuntimeError('This candidate was archived; restore the verified copy or use a new run')
        if args.build:
            index=Index(ndim=vectors.shape[1],metric='cos',dtype=args.dtype,connectivity=16,
                        expansion_add=128,expansion_search=1024,enable_key_lookups=False)
            if path.exists():index.load(path)
            count=len(index)
            if count % len(vectors):raise ValueError('Invalid checkpoint size')
            started=time.perf_counter()
            for replica in range(count//len(vectors),args.scale):
                if shutil.disk_usage(run).free<35*1024**3:raise RuntimeError('SSD reserve reached')
                if replica:
                    rng=np.random.default_rng(20260928+replica)
                    batch=np.asarray(vectors).copy()
                    for start in range(0,len(vectors),4096):
                        part=batch[start:start+4096]
                        part+=rng.normal(0,.015,part.shape).astype(np.float32)
                        part/=np.linalg.norm(part,axis=1,keepdims=True)
                else:batch=np.asarray(vectors)
                index.add(np.arange(len(vectors),dtype=np.uint64)+replica*len(vectors),batch,threads=4)
                if (replica+1)%5==0 or replica+1==args.scale:
                    if shutil.disk_usage(run).free < 35*1024**3 + index.serialized_length:
                        raise RuntimeError('SSD reserve would be crossed by the temporary atomic checkpoint')
                    temporary=path.with_suffix('.tmp')
                    index.save(temporary)
                    with temporary.open('rb') as f:os.fsync(f.fileno())
                    os.replace(temporary,path)
                    progress(run,task,stage='checkpoint',done=replica+1,total=args.scale,
                             seconds=time.perf_counter()-started,bytes=path.stat().st_size)
            result=dict(rows=len(index),bytes=path.stat().st_size,seconds=time.perf_counter()-started,
                        peak_RSS_MiB=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024**2)
            save(run/(task+'-build.json'),result)
            progress(run,task,stage='complete',**result)
            return
        started=time.perf_counter()
        index=Index(ndim=vectors.shape[1],metric='cos',dtype=args.dtype,path=path,view=True,enable_key_lookups=False)
        index.expansion_search=args.expansion
        open_ms=(time.perf_counter()-started)*1000
        queries=np.load(run/'queries.npy')
        names=json.loads((run/'queries.json').read_text())
        table=None
        if args.rerank and args.scale>1:
            from vector_bench import connect
            table=connect(run,args.scale).open_table('frames')
        results=[]
        for i,query in enumerate(queries):
            started=time.perf_counter()
            matches=index.search(query,count=args.candidates,threads=1)
            keys=np.asarray(matches.keys,dtype=np.int64)
            # At 1x rerank against real full-precision vectors. At 50x the graph
            # benchmark initially reports quantized ranking; assess separately.
            if args.scale==1:
                scores=np.asarray(vectors[keys])@query
                keys=keys[np.argsort(-scores)]
            elif table is not None:
                # Only valid for this append-only benchmark, whose offset == id.
                # Production must use stable IDs rather than physical offsets.
                batch=table.take_offsets(keys.tolist()).select(['id','vector']).to_arrow()
                matrix=batch['vector'].combine_chunks().values.to_numpy().reshape(-1,vectors.shape[1])
                scores=matrix@query
                keys=batch['id'].to_numpy()[np.argsort(-scores)]
            found=[];seen=set()
            for key in keys:
                file_id=int(metadata['file_id'][key%len(vectors)])+(key//len(vectors))*len(files)
                if file_id not in seen:
                    found.append(file_id);seen.add(file_id)
                    if len(found)==60:break
            elapsed=(time.perf_counter()-started)*1000
            recall=None
            if args.scale==1:
                scores=vectors@query
                best=np.full(len(files),-np.inf,dtype=np.float32)
                np.maximum.at(best,metadata['file_id'],scores)
                expected=set(int(k) for k in np.argsort(-best)[:60])
                recall=len(set(found)&expected)/60
            results.append(dict(query=names[i],ms=elapsed,recall60=recall,files=len(found)))
        recalls=[r['recall60'] for r in results if r['recall60'] is not None]
        result=dict(open_ms=open_ms,latency=percentiles([r['ms'] for r in results]),
                    mean_recall60=sum(recalls)/len(recalls) if recalls else None,min_recall60=min(recalls) if recalls else None,
                    peak_RSS_MiB=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024**2,
                    candidates=args.candidates,expansion=args.expansion,rerank=(args.scale==1 or args.rerank),requests=results,
                    caveat='Memory-mapped graph, process-open with potentially warm OS cache; metadata filters not yet implemented.')
        save(run/f'{task}-benchmark-{time.time_ns()}.json',result)
        print(json.dumps({k:v for k,v in result.items() if k!='requests'},indent=2),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,required=True)
    parser.add_argument('--scale',type=int,default=1)
    parser.add_argument('--build',action='store_true')
    parser.add_argument('--candidates',type=int,default=2048)
    parser.add_argument('--expansion',type=int,default=2048)
    parser.add_argument('--rerank',action='store_true')
    parser.add_argument('--dtype',choices=['i8','f16'],default='i8')
    args=parser.parse_args()
    try:
        main(args)
    except BlockingIOError:
        raise  # Another owner keeps its own progress record.
    except Exception as error:
        if args.build:
            task=f'graph-{args.scale}x'+('' if args.dtype=='i8' else '-'+args.dtype)
            path=args.run/(task+'-progress.json')
            previous=json.loads(path.read_text()) if path.exists() else {}
            progress(args.run,task,stage='stopped',error=str(error),total=args.scale,
                     checkpoint_replica=previous.get('done',previous.get('checkpoint_replica',0)))
        raise
