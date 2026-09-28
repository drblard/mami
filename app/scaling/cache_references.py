"""Retain compact exact baselines so the large rejected experiment can be archived."""
import argparse
import hashlib
import json
import os
from pathlib import Path
from common import lock, save, progress

os.environ.setdefault('RAYON_NUM_THREADS','4')
os.environ.setdefault('OPENBLAS_NUM_THREADS','4')


def narrow_name(scale, condition):
    return f'narrow-{scale}x-'+hashlib.sha256(condition.encode()).hexdigest()[:16]+'.npz'


def main(args):
    import numpy as np
    from vector_bench import base_data, connect, unique_files
    with lock(args.run,'reference-cache') as run:
        vectors,metadata,files,cameras=base_data(run)
        table=connect(run,args.scale).open_table('frames')
        queries=np.load(run/'queries.npy')
        camera=int(np.bincount(metadata['camera']).argmax())
        days=sorted(set(metadata['day'])-{0});day=int(days[len(days)//2])
        conditions=[(None,np.ones(len(vectors),dtype=bool)),
                    (f'camera = {camera}',metadata['camera']==camera),
                    (f'day = {day}',metadata['day']==day)]
        paths=[]
        for condition,mask in conditions:
            if condition and int(mask.sum())*args.scale<=50000:
                target=run/narrow_name(args.scale,condition)
                if not target.exists():
                    batch=table.search().where(condition).limit(int(mask.sum())*args.scale).select(['file_id','vector']).to_arrow()
                    matrix=batch['vector'].combine_chunks().values.to_numpy().reshape(-1,vectors.shape[1])
                    with target.with_suffix('.tmp').open('wb') as output:
                        np.savez(output,vectors=matrix,file_ids=batch['file_id'].to_numpy())
                        output.flush();os.fsync(output.fileno())
                    os.replace(target.with_suffix('.tmp'),target)
                paths.append(target.name)
            for i in range(len(queries)) if args.all_queries else [0,1,2,7,10,11,14,20]:
                cache=run/('exact-files-'+hashlib.sha256(f'{args.scale}:{i}:{condition}'.encode()).hexdigest()[:16]+'.json')
                if not cache.exists():
                    search=table.search(queries[i]).metric('cosine').bypass_vector_index().limit(4096).select(['id','file_id','_distance'])
                    if condition:search=search.where(condition,prefilter=True)
                    save(cache,sorted(set(unique_files(search.to_list()))))
                paths.append(cache.name)
                progress(run,'reference-cache',stage='cache',query=i,filter=condition,files=len(paths))
        result=dict(scale=args.scale,source_version=table.version,source_rows=table.count_rows(),files=paths,
                    query_sha256=hashlib.sha256((run/'queries.npy').read_bytes()).hexdigest())
        save(run/'reference-cache.json',result)
        progress(run,'reference-cache',stage='complete',**result)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,required=True)
    parser.add_argument('--scale',type=int,default=50)
    parser.add_argument('--all-queries',action='store_true')
    main(parser.parse_args())
