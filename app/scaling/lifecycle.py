"""Exercise idempotent vector commits, updates, deletes and reader refresh in isolation."""
import argparse
from dataclasses import asdict
from pathlib import Path
import time
from common import lock, progress, save


def main(args):
    import lancedb
    import numpy as np
    import pyarrow as pa
    with lock(args.run, 'lifecycle') as run:
        directory=run/f'lifecycle-{time.time_ns()}'
        db=lancedb.connect(directory)
        vectors=np.load(run/'vectors.npy',mmap_mode='r')[:4096]
        arrays=[pa.array(np.arange(len(vectors))),pa.FixedSizeListArray.from_arrays(pa.array(vectors.ravel()),vectors.shape[1])]
        table=db.create_table('frames',pa.Table.from_arrays(arrays,names=['id','vector']))
        table.create_index(metric='cosine',num_partitions=16,num_sub_vectors=96,max_iterations=5,sample_rate=64)
        reader=lancedb.connect(directory).open_table('frames')
        initial=table.count_rows()
        query=np.zeros(vectors.shape[1],dtype=np.float32);query[0]=1
        item=dict(id=999999,vector=query.tolist())
        # Replay after a simulated crash between the vector commit and outbox ack.
        for _ in range(2):
            table.merge_insert('id').when_matched_update_all().when_not_matched_insert_all().execute([item])
        assert table.count_rows()==initial+1,'Replayed upsert duplicated a row'
        reader.checkout_latest()
        assert reader.search(query).metric('cosine').limit(1).to_list()[0]['id']==item['id'],'New unindexed row missing'
        added_stats=asdict(table.index_stats('vector_idx'))
        changed=np.zeros_like(query);changed[1]=1
        item['vector']=changed.tolist()
        table.merge_insert('id').when_matched_update_all().when_not_matched_insert_all().execute([item])
        reader.checkout_latest()
        assert reader.search(changed).metric('cosine').limit(1).to_list()[0]['id']==item['id'],'Replacement missing'
        table.optimize()
        reader.checkout_latest()
        optimized_stats=asdict(table.index_stats('vector_idx'))
        assert reader.search(changed).metric('cosine').limit(1).to_list()[0]['id']==item['id'],'Maintenance lost update'
        table.delete('id = 999999')
        reader.checkout_latest()
        assert reader.count_rows()==initial,'Delete failed'
        assert all(row['id']!=999999 for row in reader.search(changed).limit(60).to_list()),'Deleted row searchable'
        reopened=lancedb.connect(directory).open_table('frames')
        assert reopened.count_rows()==initial,'Restart lost catalog version'
        result=dict(status='passed',directory=str(directory),checks=['idempotent replay','unindexed arrival','replacement',
                     'separate reader refresh','optimize preserves results','delete','reopen'],
                    after_add=added_stats,after_optimize=optimized_stats)
        save(run/'lifecycle.json',result)
        progress(run,'lifecycle',stage='complete',**result)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,required=True)
    main(parser.parse_args())
