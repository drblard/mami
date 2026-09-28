"""Rebuild only experiment indexes; retain Lance versions and benchmark records."""
import argparse
from dataclasses import asdict
from pathlib import Path
import os
import time
from common import lock, save, progress

os.environ.setdefault('RAYON_NUM_THREADS','4')


def main(args):
    import lancedb
    from lancedb.index import IvfSq, IvfPq, IvfRq
    with lock(args.run,f'vectors-{args.scale}x') as run:
        table=lancedb.connect(run/f'lance-{args.scale}x').open_table('frames')
        started=time.perf_counter()
        options=dict(distance_type='cosine',num_partitions=args.partitions,max_iterations=20,sample_rate=64)
        config=(IvfSq(**options) if args.kind=='SQ' else IvfRq(**options,num_bits=args.bits)
                if args.kind=='RQ' else IvfPq(**options,num_sub_vectors=args.sub_vectors))
        progress(run,'tune',stage='build',scale=args.scale,kind=args.kind,partitions=args.partitions)
        table.create_index('vector',config=config,name='vector_idx',replace=True)
        table.create_scalar_index('camera',index_type='BITMAP',name='camera_idx',replace=True)
        table.create_scalar_index('day',index_type='BITMAP',name='day_idx',replace=True)
        result=dict(kind=args.kind,scale=args.scale,partitions=args.partitions,sub_vectors=args.sub_vectors,bits=args.bits,
                    seconds=time.perf_counter()-started,version=table.version,index_stats=asdict(table.index_stats('vector_idx')))
        save(run/f'tune-{args.scale}x-{time.time_ns()}.json',result)
        progress(run,'tune',stage='complete',**result)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,required=True)
    parser.add_argument('--scale',type=int,default=1)
    parser.add_argument('--partitions',type=int,default=256)
    parser.add_argument('--kind',choices=['SQ','PQ','RQ'],default='SQ')
    parser.add_argument('--sub-vectors',type=int,default=192)
    parser.add_argument('--bits',type=int,default=4)
    main(parser.parse_args())
