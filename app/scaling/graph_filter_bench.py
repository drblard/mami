"""Full-scale graph filtering/recall checks with cached exact baselines and footprint."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import resource
import subprocess
import time
from common import lock, save, progress, percentiles

os.environ.setdefault('RAYON_NUM_THREADS','4')
os.environ.setdefault('OPENBLAS_NUM_THREADS','4')


def main(args):
    import numpy as np
    from usearch.index import Index
    from vector_bench import base_data, connect, unique_files
    from cache_references import narrow_name
    task=f'graph-quality-{args.scale}x-{args.dtype}'
    with lock(args.run,task) as run:
        vectors,metadata,files,cameras=base_data(run)
        suffix='' if args.dtype=='i8' else '-'+args.dtype
        started=time.perf_counter()
        index=Index(ndim=vectors.shape[1],metric='cos',dtype=args.dtype,
                    path=run/f'graph-{args.scale}x{suffix}.usearch',view=True,enable_key_lookups=False)
        index.expansion_search=args.expansion
        open_ms=(time.perf_counter()-started)*1000
        assert len(index)==len(vectors)*args.scale,'Full-scale graph is not complete'
        table=None
        def reference_table():
            nonlocal table
            if table is None:
                if not (run/f'lance-{args.scale}x/frames.lance').exists():
                    raise RuntimeError('Reference cache incomplete and raw reference table archived')
                table=connect(run,args.scale).open_table('frames')
            return table
        queries=np.load(run/'queries.npy')
        names=json.loads((run/'queries.json').read_text())
        camera=int(np.bincount(metadata['camera']).argmax())
        days=sorted(set(metadata['day'])-{0});day=int(days[len(days)//2])
        conditions=[(None,np.ones(len(vectors),dtype=bool)),
                    (f'camera = {camera}',metadata['camera']==camera),
                    (f'day = {day}',metadata['day']==day)]
        selected=[0,1,2,7,10,11,14,20]
        results=[]
        for condition,mask in conditions:
            for i in selected:
                cache=run/('exact-files-'+hashlib.sha256(f'{args.scale}:{i}:{condition}'.encode()).hexdigest()[:16]+'.json')
                if cache.exists():expected=set(json.loads(cache.read_text()))
                else:
                    search=reference_table().search(queries[i]).metric('cosine').bypass_vector_index().limit(4096).select(['id','file_id','_distance'])
                    if condition:search=search.where(condition,prefilter=True)
                    expected=set(unique_files(search.to_list()));save(cache,sorted(expected))
                started=time.perf_counter()
                if condition and int(mask.sum())*args.scale<=50000:
                    cached=run/narrow_name(args.scale,condition)
                    if cached.exists():
                        with np.load(cached) as batch:
                            scores=batch['vectors']@queries[i]
                            file_ids=batch['file_ids']
                        found=[];seen=set()
                        for ordinal in np.argsort(-scores):
                            file_id=int(file_ids[ordinal])
                            if file_id in seen:continue
                            found.append(file_id);seen.add(file_id)
                            if len(found)==60:break
                    else:
                        search=reference_table().search(queries[i]).metric('cosine').bypass_vector_index().where(condition,prefilter=True).limit(1024).select(['id','file_id','_distance'])
                        found=unique_files(search.to_list())
                    route='scoped_exact'
                else:
                    matches=index.search(queries[i],count=args.candidates,threads=1)
                    found=[];seen=set()
                    for key in matches.keys:
                        key=int(key)
                        if not mask[key%len(vectors)]:continue
                        file_id=int(metadata['file_id'][key%len(vectors)])+(key//len(vectors))*len(files)
                        if file_id in seen:continue
                        seen.add(file_id);found.append(file_id)
                        if len(found)==60:break
                    route='graph_postfilter'
                row=dict(query=names[i],filter=condition,route=route,ms=(time.perf_counter()-started)*1000,
                         files=len(found),expected=len(expected),recall60=len(set(found)&expected)/len(expected) if expected else 1)
                results.append(row)
                save(run/(task+'-partial.json'),results)
                progress(run,task,stage='query',done=len(results),total=len(selected)*len(conditions),**row)
        summaries=[]
        for condition,_ in conditions:
            subset=[r for r in results if r['filter']==condition]
            summaries.append(dict(filter=condition,**percentiles([r['ms'] for r in subset]),
                                  mean_recall60=sum(r['recall60'] for r in subset)/len(subset),
                                  min_recall60=min(r['recall60'] for r in subset)))
        stamp=time.time_ns()
        vmmap=subprocess.run(['vmmap','-summary',str(os.getpid())],capture_output=True,text=True,timeout=30)
        (run/f'{task}-vmmap-{stamp}.txt').write_text(vmmap.stdout+vmmap.stderr)
        footprint=[line.strip() for line in vmmap.stdout.splitlines() if 'footprint' in line]
        result=dict(open_ms=open_ms,candidates=args.candidates,expansion=args.expansion,summaries=summaries,
                    peak_RSS_MiB=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024**2,
                    footprint=footprint,requests=results,
                    caveat='Small difficult-query sample, warm cache possible, exact baselines interleaved can perturb cache. Filters use benchmark metadata.')
        save(run/f'{task}-{stamp}.json',result)
        progress(run,task,stage='complete',**{k:v for k,v in result.items() if k!='requests'})


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,required=True)
    parser.add_argument('--scale',type=int,default=50)
    parser.add_argument('--dtype',choices=['i8','f16'],default='f16')
    parser.add_argument('--candidates',type=int,default=4096)
    parser.add_argument('--expansion',type=int,default=8192)
    main(parser.parse_args())
