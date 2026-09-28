"""Test file-candidate retrieval from temporal representatives against every frame."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import time
from common import lock, save, progress

os.environ.setdefault('OPENBLAS_NUM_THREADS','4')


def main(args):
    import numpy as np
    from vector_bench import base_data
    with lock(args.run,'scenes') as run:
        vectors,metadata,files,cameras=base_data(run)
        queries=np.load(run/'queries.npy')
        with sqlite3.connect(run/'corpus.sqlite') as db:
            times=np.asarray([row[0] or 0 for row in db.execute('SELECT timestamp FROM frames ORDER BY id')])
        order=np.lexsort((times,metadata['file_id']))
        baselines=[]
        for query in queries:
            best=np.full(len(files),-np.inf,dtype=np.float32)
            np.maximum.at(best,metadata['file_id'],vectors@query)
            baselines.append(set(np.argsort(-best)[:60]))
        results=[]
        for threshold in (.995,.99,.98,.95,.9,.85,.8):
            selected=[];last=None
            for i in order:
                if last is None or metadata['file_id'][i]!=metadata['file_id'][last] or times[i]-times[last]>=30 or float(vectors[i]@vectors[last])<threshold:
                    selected.append(int(i));last=i
            np.save(run/f'scene-ids-{threshold}.npy',np.asarray(selected,dtype=np.int64))
            scores=np.asarray(vectors[selected])@queries.T
            for candidates in (120,240,480):
                recall=[]
                for i,expected in enumerate(baselines):
                    best=np.full(len(files),-np.inf,dtype=np.float32)
                    np.maximum.at(best,metadata['file_id'][selected],scores[:,i])
                    found=set(np.argsort(-best)[:candidates])
                    recall.append(len(found&expected)/len(expected))
                results.append(dict(threshold=threshold,representatives=len(selected),fraction=len(selected)/len(vectors),
                                    file_candidates=candidates,mean_recall60=sum(recall)/len(recall),min_recall60=min(recall)))
        save(run/'scenes.json',results)
        progress(run,'scenes',stage='complete',results=results)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,required=True)
    main(parser.parse_args())
