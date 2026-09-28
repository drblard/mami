"""Time a fresh search-side process without ever reading the full vector corpus."""
import argparse
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from common import save


def main(args):
    started=time.perf_counter()
    db=sqlite3.connect((args.run/f'text-{args.scale}x.sqlite').resolve().as_uri()+'?mode=ro',uri=True)
    db.execute('PRAGMA cache_size=-32768')
    text=db.execute("SELECT rowid FROM speech_fast WHERE speech_fast MATCH ? ORDER BY rowid DESC LIMIT 60",('dun*',)).fetchall()
    text_ready=(time.perf_counter()-started)*1000
    import lancedb
    from tokenizers import Tokenizer
    import numpy as np
    session=lancedb.Session(index_cache_size_bytes=512*1024**2,metadata_cache_size_bytes=128*1024**2)
    table=lancedb.connect(args.run/f'lance-{args.scale}x',session=session).open_table('frames')
    tokenizer=Tokenizer.from_file(str(args.run/'text-encoder/tokenizer.json'))
    tokens=tokenizer.encode('milking goats').ids
    visual_ready=(time.perf_counter()-started)*1000
    query=np.load(args.run/'queries.npy')[1]
    before=time.perf_counter()
    hits=table.search(query).metric('cosine').nprobes(128).refine_factor(1).limit(1024).select(['id','file_id','_distance']).to_list()
    first_query=(time.perf_counter()-before)*1000
    result=dict(text_ready_ms=text_ready,visual_storage_and_tokenizer_ready_ms=visual_ready,
                first_vector_query_ms=first_query,text_rows=len(text),candidates=len(hits),
                caveat='Fresh Python process, warm OS cache possible. Native encoder is measured separately; not a full app startup measurement.')
    save(args.run/f'startup-{args.scale}x-{time.time_ns()}.json',result)
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,required=True)
    parser.add_argument('--scale',type=int,default=50)
    main(parser.parse_args())
