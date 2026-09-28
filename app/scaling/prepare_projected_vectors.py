"""Create resumable float16 projection/full-vector files for exact GPU scan trials."""
import argparse
import json
import os
from pathlib import Path
import time
from common import lock, save, progress

os.environ.setdefault('OPENBLAS_NUM_THREADS', '4')


def main(args):
    import numpy as np
    with lock(args.run, 'projected-vectors') as run:
        vectors = np.load(run/'vectors.npy', mmap_mode='r')
        basis = np.load(run/'pca-basis.npz')
        transform = basis['basis'][:, :args.dimensions]
        mean = basis['mean']
        queries = np.load(run/'queries.npy')@transform
        (run/'projected-queries.json').write_text(json.dumps(queries.tolist()))
        state_file = run/'projected-state.json'
        state = json.loads(state_file.read_text()) if state_file.exists() else dict(done=0, dimensions=args.dimensions, scale=args.scale)
        if state['dimensions'] != args.dimensions or state['scale'] != args.scale:
            raise ValueError('Projection settings changed')
        projected_path, full_path = run/'projected.f16', run/'full.f16'
        with projected_path.open('a+b') as projected, full_path.open('a+b') as full:
            projected.truncate(state['done']*len(vectors)*args.dimensions*2)
            full.truncate(state['done']*vectors.nbytes//2)
            for replica in range(state['done'], args.scale):
                rng = np.random.default_rng(20260928+replica)
                for start in range(0, len(vectors), 4096):
                    batch = np.asarray(vectors[start:start+4096]).copy()
                    if replica:
                        batch += rng.normal(0,.015,batch.shape).astype(np.float32)
                        batch /= np.linalg.norm(batch,axis=1,keepdims=True)
                    full.write(batch.astype(np.float16).tobytes())
                    projected.write(((batch-mean)@transform).astype(np.float16).tobytes())
                for file in (projected, full):
                    file.flush();os.fsync(file.fileno())
                state['done'] = replica+1
                save(state_file, state)
                progress(run, 'projected-vectors', stage='write', **state)
        save(run/'projected-shape.json', dict(rows=len(vectors)*args.scale, dimensions=args.dimensions))


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,required=True)
    parser.add_argument('--scale',type=int,default=50)
    parser.add_argument('--dimensions',type=int,default=128)
    main(parser.parse_args())
