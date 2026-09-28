"""End-to-end projected scan / exact rerank benchmark, including top-k and I/O."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from common import save, percentiles

os.environ.setdefault('OPENBLAS_NUM_THREADS','4')


def main(args):
    import numpy as np
    from vector_bench import base_data
    vectors, metadata, files, cameras = base_data(args.run)
    full = np.memmap(args.run/'full.f16', dtype=np.float16, mode='r', shape=(len(vectors)*50,768))
    queries = np.load(args.run/'queries.npy')
    projected = json.loads((args.run/'projected-queries.json').read_text())
    process = subprocess.Popen([str((args.run/'projected-scan').resolve()), str(args.run.resolve()), *(['--full'] if args.full else [])],
                               stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
    results=[]
    grouped = np.argsort(metadata['file_id'], kind='stable')
    starts = np.flatnonzero(np.r_[True, np.diff(metadata['file_id'][grouped]) != 0])
    started=time.perf_counter()
    try:
        assert json.loads(process.stdout.readline())['ready']
        ready_seconds=time.perf_counter()-started
        for i in [0,1,2,7,10,11,14,20]:
            started=time.perf_counter()
            process.stdin.write(json.dumps(dict(vector=queries[i].tolist() if args.full else projected[i]))+'\n');process.stdin.flush()
            timing=json.loads(process.stdout.readline())
            scores=np.memmap(args.run/'scores.f32',dtype=np.float32,mode='r')
            if args.full:
                maxima = np.concatenate([np.maximum.reduceat(scores[replica*len(vectors)+(grouped)], starts) for replica in range(50)])
                selected = np.argpartition(maxima,-60)[-60:]
                found = selected[np.argsort(-maxima[selected])].tolist()
            else:
                candidates=np.argpartition(scores,-8192)[-8192:]
                exact=full[candidates].astype(np.float32)@queries[i]
                ordered=candidates[np.argsort(-exact)]
                found=[];seen=set()
                for key in ordered:
                    asset=int(metadata['file_id'][key%len(vectors)])+int(key//len(vectors))*len(files)
                    if asset not in seen:
                        found.append(asset);seen.add(asset)
                        if len(found)==60:break
            elapsed=(time.perf_counter()-started)*1000
            cache=args.reference/('exact-files-'+hashlib.sha256(f'50:{i}:None'.encode()).hexdigest()[:16]+'.json')
            expected=set(json.loads(cache.read_text()))
            results.append(dict(query=i,ms=elapsed,scan_ms=timing['seconds']*1000,recall60=len(set(found)&expected)/60))
            del scores
        result=dict(full_scan=args.full,ready_seconds=ready_seconds,latency=percentiles([r['ms'] for r in results]),requests=results,
                    mean_recall=sum(r['recall60'] for r in results)/len(results),minimum_recall=min(r['recall60'] for r in results),
                    caveat='Experimental projected exhaustive GPU scan, FP16 exact reranking; synthetic corpus, warm OS cache possible, no CapCut contention test.')
        save(args.run/f'projected-scan-results-{time.time_ns()}.json',result)
        print(json.dumps(result,indent=2),flush=True)
    finally:
        process.stdin.close()
        try:process.wait(timeout=10)
        except subprocess.TimeoutExpired:process.kill();process.wait()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,required=True)
    parser.add_argument('--reference',type=Path,required=True)
    parser.add_argument('--full',action='store_true')
    main(parser.parse_args())
