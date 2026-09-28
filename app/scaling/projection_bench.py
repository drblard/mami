"""Evaluate reduced-dimensional full scans before committing to another ANN engine."""
import argparse
import os
from pathlib import Path
import time
from common import lock, save, progress

os.environ.setdefault('OPENBLAS_NUM_THREADS', '4')


def main(args):
    import numpy as np
    from vector_bench import base_data
    with lock(args.run, 'projection') as run:
        vectors, metadata, files, cameras = base_data(run)
        queries = np.load(run/'queries.npy')
        mean = np.mean(vectors, axis=0)
        covariance = np.zeros((vectors.shape[1], vectors.shape[1]), dtype=np.float64)
        for start in range(0, len(vectors), 4096):
            centered = vectors[start:start+4096]-mean
            covariance += centered.T@centered
        eigenvalues, eigenvectors = np.linalg.eigh(covariance)
        basis = eigenvectors[:, ::-1].astype(np.float32)
        np.savez(run/'pca-basis.npz', mean=mean, basis=basis, eigenvalues=eigenvalues[::-1])
        expected = []
        for query in queries:
            best = np.full(len(files), -np.inf, dtype=np.float32)
            np.maximum.at(best, metadata['file_id'], vectors@query)
            expected.append(set(int(value) for value in np.argsort(-best)[:60]))
        results = []
        for dimensions in (64, 96, 128, 192, 256):
            compressed = ((vectors-mean)@basis[:, :dimensions]).astype(np.float16)
            for candidates in (1024, 4096, 8192):
                recalls, timings = [], []
                for i, query in enumerate(queries):
                    started = time.perf_counter()
                    scores = compressed.astype(np.float32)@(query@basis[:, :dimensions])
                    selected = np.argpartition(scores, -candidates)[-candidates:]
                    exact = vectors[selected]@query
                    selected = selected[np.argsort(-exact)]
                    found = []
                    seen = set()
                    for index in selected:
                        asset = int(metadata['file_id'][index])
                        if asset not in seen:
                            seen.add(asset); found.append(asset)
                            if len(found) == 60:
                                break
                    recalls.append(len(set(found)&expected[i])/60)
                    timings.append((time.perf_counter()-started)*1000)
                results.append(dict(dimensions=dimensions, candidates=candidates, mean_recall=sum(recalls)/len(recalls),
                                    minimum_recall=min(recalls), peak_ms=max(timings), fifty_x_bytes=compressed.nbytes*50))
        save(run/'projection-quality.json', results)
        progress(run, 'projection', stage='complete', results=results)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    main(parser.parse_args())
