"""Evaluate MLX's supported groupwise quantized matmul as a full-scan candidate."""
import argparse
import json
import os
from pathlib import Path
import time
from common import save, progress, percentiles

os.environ.setdefault('OPENBLAS_NUM_THREADS','4')


def main(args):
    import numpy as np
    import mlx.core as mx
    from vector_bench import base_data
    mx.set_cache_limit(128*1024**2)
    vectors,metadata,files,cameras=base_data(args.run)
    queries=np.load(args.run/'queries.npy')
    suffix='-half-scales' if args.half_scales else ''
    artifact=args.run/f'mlx-{args.scale}x-{args.bits}bit{suffix}.safetensors'
    if not artifact.exists():
        count=len(vectors)*args.scale
        # Allocate final packed arrays in NumPy, quantize only one replica at a time.
        packed=np.empty((count,768*args.bits//32),dtype=np.uint32)
        scales=np.empty((count,768//64),dtype=np.float16 if args.half_scales else np.float32)
        biases=np.empty_like(scales)
        for replica in range(args.scale):
            batch=np.asarray(vectors).copy()
            if replica:
                rng=np.random.default_rng(20260928+replica)
                for start in range(0,len(batch),4096):
                    part=batch[start:start+4096]
                    part+=rng.normal(0,.015,part.shape).astype(np.float32)
                    part/=np.linalg.norm(part,axis=1,keepdims=True)
            q,s,b=mx.quantize(mx.array(batch),group_size=64,bits=args.bits)
            mx.eval(q,s,b)
            offset=replica*len(vectors)
            packed[offset:offset+len(vectors)]=np.array(q)
            scales[offset:offset+len(vectors)]=np.array(s)
            biases[offset:offset+len(vectors)]=np.array(b)
            progress(args.run,'mlx-quantize',done=replica+1,total=args.scale)
        mx.save_safetensors(str(artifact),dict(weight=mx.array(packed),scales=mx.array(scales),biases=mx.array(biases)))
        del packed,scales,biases,batch,q,s,b
        mx.clear_cache()
    weights=mx.load(str(artifact))
    mx.eval(*weights.values())
    results=[]
    reference_path=args.run/'full.f32'
    if args.scale>1 and args.reference and not reference_path.exists():
        temporary=reference_path.with_suffix('.tmp')
        with temporary.open('wb') as target:
            for replica in range(args.scale):
                rng=np.random.default_rng(20260928+replica)
                for start in range(0,len(vectors),4096):
                    batch=np.asarray(vectors[start:start+4096]).copy()
                    if replica:
                        batch+=rng.normal(0,.015,batch.shape).astype(np.float32)
                        batch/=np.linalg.norm(batch,axis=1,keepdims=True)
                    target.write(batch.tobytes())
            target.flush();os.fsync(target.fileno())
        os.replace(temporary,reference_path)
    full=np.memmap(reference_path,dtype=np.float32,mode='r',shape=(len(vectors)*args.scale,768)) if args.reference else None
    for i,query in enumerate(queries):
        started=time.perf_counter()
        scores=mx.quantized_matmul(mx.array(query[None,:],dtype=weights['scales'].dtype),weights['weight'],weights['scales'],weights['biases'],transpose=True,group_size=64,bits=args.bits)
        mx.eval(scores)
        scores=np.array(scores).ravel()
        ids=np.argpartition(scores,-args.candidates)[-args.candidates:]
        if args.scale==1:
            exact=vectors[ids]@query
            order=ids[np.argsort(-exact)]
            found=[];seen=set()
            for index in order:
                file_id=int(metadata['file_id'][index])
                if file_id not in seen:
                    seen.add(file_id);found.append(file_id)
                    if len(found)==60:break
            elapsed=(time.perf_counter()-started)*1000
            all_scores=vectors@query
            best=np.full(len(files),-np.inf,dtype=np.float32)
            np.maximum.at(best,metadata['file_id'],all_scores)
            expected=set(np.argsort(-best)[:60])
            recall=len(set(found)&expected)/60
        else:
            recall=None
            if full is not None:
                exact=full[ids]@query
                order=ids[np.argsort(-exact)]
                found=[];seen=set()
                for index in order:
                    asset=int(metadata['file_id'][index%len(vectors)])+int(index//len(vectors))*len(files)
                    if asset not in seen:
                        seen.add(asset);found.append(asset)
                        if len(found)==60:break
                elapsed=(time.perf_counter()-started)*1000
                import hashlib
                cache=args.reference/('exact-files-'+hashlib.sha256(f'{args.scale}:{i}:None'.encode()).hexdigest()[:16]+'.json')
                if cache.exists():
                    expected=set(json.loads(cache.read_text()))
                    recall=len(set(found)&expected)/60
            else:
                elapsed=(time.perf_counter()-started)*1000
        results.append(dict(query=i,ms=elapsed,recall60=recall))
    result=dict(bits=args.bits,half_scales=args.half_scales,scale=args.scale,candidates=args.candidates,latency=percentiles([r['ms'] for r in results]),
                active_MiB=mx.get_active_memory()/1024**2,cache_MiB=mx.get_cache_memory()/1024**2,
                artifact_bytes=artifact.stat().st_size,requests=results)
    recalls=[r['recall60'] for r in results if r['recall60'] is not None]
    if recalls:
        result.update(mean_recall=sum(recalls)/len(recalls),minimum_recall=min(recalls),recall_queries=len(recalls))
    result['exact_rerank']=args.reference is not None or args.scale==1
    save(args.run/f'mlx-{args.scale}x-{args.bits}bit-results-{time.time_ns()}.json',result)
    print(json.dumps(result,indent=2),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,required=True)
    parser.add_argument('--scale',type=int,default=1)
    parser.add_argument('--bits',type=int,default=4)
    parser.add_argument('--candidates',type=int,default=4096)
    parser.add_argument('--reference',type=Path)
    parser.add_argument('--half-scales',action='store_true')
    main(parser.parse_args())
