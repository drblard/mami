"""Exercise real packed-search processes against an isolated, prepared catalog."""
import argparse
import contextlib
import hashlib
import json
from pathlib import Path
import select
import subprocess
import sys
import time


def main(args):
    root=args.fixture.resolve()
    if 'benchmarks' not in root.parts:
        raise ValueError('Lifecycle checks require an isolated benchmark fixture')
    output=root/('lifecycle-'+str(time.time_ns()));output.mkdir()
    resources=args.bundle/'Contents/Resources'
    sys.path.insert(0,str(resources))
    import numpy as np
    from index_store import connection
    from model_config import PIPELINE
    from text_encoder import NativeEncoder
    source=root/'catalog/catalog.sqlite';projection=root/'search.sqlite';vectors=root/'vectors'
    personal=root/'catalog/user.sqlite'
    personal_before=hashlib.sha256(personal.read_bytes()).hexdigest() if personal.exists() else None
    command=[sys.executable,'-B',str(resources/'search_worker.py'),'--index',str(args.index),
             '--packed-index',str(vectors),'--projection',str(projection),'--native-encoder',str(args.encoder)]
    workers=[]
    with contextlib.ExitStack() as stack:
        def start_search():
            log=stack.enter_context((output/f'search-{len(workers)}.log').open('x'))
            process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=log,text=True)
            workers.append(process)
            read(process,60)
            return process
        def read(process,timeout=15):
            if not select.select([process.stdout],[],[],timeout)[0]:raise TimeoutError('Search worker did not reply')
            reply=json.loads(process.stdout.readline())
            if 'error' in reply:raise RuntimeError(reply['error'])
            return reply
        def query(process,text='milking goats',**options):
            deadline=time.monotonic()+30
            while True:
                process.stdin.write(json.dumps(dict(query=text,mode='visual',**options))+'\n');process.stdin.flush()
                reply=read(process)
                if not reply.get('visual_pending'):return reply
                if time.monotonic()>deadline:raise TimeoutError('Visual search warmup deadline')
                time.sleep(.05)
        def until(process,predicate,**options):
            deadline=time.monotonic()+20
            while time.monotonic()<deadline:
                reply=query(process,**options)
                if predicate(reply):return reply
                time.sleep(.1)
            raise TimeoutError('Committed change did not become searchable')
        def stop(process):
            process.stdin.close()
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:process.kill();process.wait()
        sync_log=stack.enter_context((output/'sync.log').open('x'))
        sync=subprocess.Popen([sys.executable,'-B',str(resources/'search_sync.py'),'--catalog',str(source),
                               '--output',str(projection),'--index',str(args.index),'--speech',str(args.speech),
                               '--vector-root',str(vectors)],stdin=subprocess.PIPE,stdout=sync_log,stderr=sync_log)
        workers.append(sync)
        try:
            process=start_search()
            initial=query(process)
            frame=initial['hits'][0]['frame']
            encoder=NativeEncoder(args.bundle/'Contents/MacOS/Mami',args.encoder)
            try:feature=encoder.encode('milking goats')
            finally:encoder.close()
            asset='fixture:'+output.name
            logical='library:'+asset
            path=str(output/'offline-original.mp4')
            vector=output/'positive.npy';np.save(vector,feature)
            negative=output/'negative.npy';np.save(negative,-feature)
            sample=dict(path=logical,kind='video',timestamp=.5,frame=frame)
            media=dict(path=logical,kind='video',url=Path(path).as_uri(),frames=[sample],match=sample,assetID=asset,
                       metadata=dict(date='Fixture',sortDate='20260929120000',details=[],tags=[],technical=[],camera='Fixture',source='iCloud'))
            def insert():
                with connection(source) as db:
                    db.execute('INSERT OR REPLACE INTO media VALUES(?,?,?)',(path,asset,json.dumps(media)))
                    db.execute('INSERT OR REPLACE INTO index_units VALUES(?,?,?,?,?)',(asset,PIPELINE,'embedding',0,json.dumps(dict(vector=str(vector),sample=sample))))
                    db.execute('INSERT OR REPLACE INTO index_units VALUES(?,?,?,?,?)',(asset,PIPELINE,'speech',0,json.dumps(dict(path=logical,segments=[dict(start=.5,text='durable search marker')]))))
            inserted=time.monotonic();insert()
            until(process,lambda r:r['hits'] and r['hits'][0]['path']==logical)
            insert_seconds=time.monotonic()-inserted
            with connection(source) as db:
                db.execute("UPDATE index_units SET payload=? WHERE asset=? AND stage='embedding'",(json.dumps(dict(vector=str(negative),sample=sample)),asset))
            until(process,lambda r:r['hits'] and r['hits'][0]['score']<-.9,paths=[logical])
            with connection(source) as db:
                db.execute('DELETE FROM media WHERE asset=?',(asset,))
                db.execute('DELETE FROM index_units WHERE asset=?',(asset,))
            until(process,lambda r:not r['hits'],paths=[logical])
            insert();until(process,lambda r:r['hits'] and r['hits'][0]['score']>.9,paths=[logical])
            build_log=stack.enter_context((output/'compaction.log').open('x'))
            compaction=subprocess.Popen([sys.executable,'-B',str(resources/'vector_sync.py'),'--projection',str(projection),
                                         '--output',str(vectors),'--once'],stdout=build_log,stderr=build_log)
            latencies=[]
            deadline=time.monotonic()+180
            while compaction.poll() is None:
                if time.monotonic()>deadline:compaction.kill();compaction.wait();raise TimeoutError('Compaction deadline')
                started=time.monotonic();reply=query(process);latencies.append((time.monotonic()-started)*1000)
                assert reply['hits'] and reply['hits'][0]['path']==logical and reply['hits'][0]['score']>.9
                time.sleep(.2)
            if compaction.returncode:raise RuntimeError('Compaction failed; see retained log')
            until(process,lambda r:r['indexed_samples']==initial['indexed_samples']+1 and r['hits'][0]['path']==logical)
            stop(process)
            process=start_search()
            assert query(process,paths=[logical])['hits'][0]['score']>.9
            process.stdin.write(json.dumps(dict(query='durable search marker',mode='speech'))+'\n');process.stdin.flush()
            assert read(process)['hits'][0]['path']==logical
            if personal_before is not None:assert hashlib.sha256(personal.read_bytes()).hexdigest()==personal_before
            result=dict(status='passed',insert_visible_seconds=insert_seconds,compaction_queries=len(latencies),
                        max_compaction_query_ms=max(latencies,default=0),personal_database_unchanged=True,
                        checks=['live insert','replacement','delete','reinsert','search during compaction','hot generation switch','restart','transcript refresh'])
            (output/'result.json').write_text(json.dumps(result,indent=2));print(json.dumps(result,indent=2),flush=True)
        finally:
            for process in reversed(workers):
                if process.poll() is None:stop(process)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle',type=Path,required=True)
    parser.add_argument('--fixture',type=Path,required=True)
    parser.add_argument('--index',type=Path,required=True)
    parser.add_argument('--speech',type=Path,required=True)
    parser.add_argument('--encoder',type=Path,required=True)
    main(parser.parse_args())
