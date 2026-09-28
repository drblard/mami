"""Persistent, local JSON-lines search worker for the native prototype."""
import argparse
import contextlib
import json
import sys
import time
import sqlite3
import os
import threading
from functools import lru_cache
from pathlib import Path
from model_config import PIPELINE, VISUAL_CPU_THREADS, cached_model_path, configure_cache_environment
from search_logic import SpeechIndex, RESULT_LIMIT, combine_hits

# Set before importing NumPy/PyTorch, including in the refresh thread.
os.environ.setdefault('OPENBLAS_NUM_THREADS', '4')
os.environ.setdefault('OMP_NUM_THREADS', '4')


class Snapshot:
    """Immutable, query-ready arrays and postings; never does file I/O."""

    def __init__(self, samples, vectors, segments):
        import numpy as np
        order = sorted(range(len(samples)), key=lambda i: (samples[i]['path'], samples[i]['timestamp'] or 0))
        self.samples = [samples[i] for i in order]
        self.vectors = np.asarray(vectors, dtype=np.float32)[order]
        self.by_path = {}
        starts = []
        for i, sample in enumerate(self.samples):
            if sample['path'] not in self.by_path:
                starts.append(i)
            self.by_path.setdefault(sample['path'], []).append(sample)
        self.paths = list(self.by_path)
        self.starts = np.asarray(starts, dtype=np.int64)
        self.ends = np.asarray(starts[1:] + [len(samples)], dtype=np.int64)
        self.speech = SpeechIndex(segments, self.by_path)

    def spoken(self, query, allowed=None):
        return self.speech.search(query, allowed)

    def visual(self, feature, allowed=None):
        import numpy as np
        if not self.samples:
            return []
        scores = self.vectors @ feature
        # Rank files, rather than sorting every frame and then discarding duplicates.
        best = np.maximum.reduceat(scores, self.starts)
        eligible = np.asarray([i for i, path in enumerate(self.paths)
                               if allowed is None or path in allowed], dtype=np.int64)
        ranked = eligible[np.argsort(-best[eligible], kind='stable')[:RESULT_LIMIT]]
        hits = []
        for group in ranked:
            start, end = self.starts[group], self.ends[group]
            i = int(start + np.argmax(scores[start:end]))
            hits.append({**self.samples[i], 'score': float(scores[i])})
        return hits


class CatalogIndex:
    """Refresh away from the request loop and atomically publish complete snapshots.

    Payload identity catches replacements/deletions, unlike a rowid watermark.
    Unchanged vector files are opened once per worker lifetime, not per query.
    """

    def __init__(self, samples, vectors, segments, catalog=None):
        self.base = samples, vectors, segments
        self.catalog = catalog
        self.token = None
        self.units = {}
        self.snapshot = Snapshot(*self.base)
        self.stop = threading.Event()

    def refresh(self):
        import numpy as np
        if not self.catalog:
            return False
        with contextlib.closing(sqlite3.connect(Path(self.catalog).resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
            db.execute('BEGIN')
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='index_units'").fetchone():
                return False
            token = db.execute('SELECT change_token FROM state').fetchone()[0]
            if token == self.token:
                return False
            rows = db.execute("SELECT asset,stage,ordinal,payload FROM index_units WHERE pipeline=? AND stage IN ('embedding','speech') AND asset IN (SELECT asset FROM media)", (PIPELINE,)).fetchall()
        units = {}
        for asset, stage, ordinal, payload in rows:
            if self.stop.is_set():
                return False
            key = asset, stage, ordinal
            previous = self.units.get(key)
            if previous is not None and previous[0] == payload:
                units[key] = previous
                continue
            value = json.loads(payload)
            if stage == 'embedding':
                try:
                    if not Path(value['sample']['frame']).is_file():
                        continue
                    vector = np.load(value['vector'], allow_pickle=False)
                except FileNotFoundError:
                    continue
                units[key] = (payload, value['sample'], vector)
            else:
                units[key] = (payload, [(value['path'], segment) for segment in value['segments']])
        changed = units.keys() != self.units.keys() or any(value is not self.units.get(key) for key, value in units.items())
        if changed:
            samples, vectors, segments = self.base
            extra = [value for key, value in units.items() if key[1] == 'embedding']
            speech = [segment for key, value in units.items() if key[1] == 'speech' for segment in value[1]]
            merged = np.concatenate([vectors, np.stack([value[2] for value in extra])]) if extra else vectors
            snapshot = Snapshot(samples + [value[1] for value in extra], merged, segments + speech)
            self.units = units
            self.snapshot = snapshot
        self.token = token
        return changed

    def run(self):
        while not self.stop.wait(5):
            try:
                self.refresh()
            except Exception as error:
                # Keep the last complete index usable if a refresh fails.
                print(f'Search index refresh: {error}', file=sys.stderr, flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--index', required=True)
    parser.add_argument('--speech')
    parser.add_argument('--catalog')
    parser.add_argument('--native-encoder', help='Validated compiled SigLIP text artifact directory')
    parser.add_argument('--packed-index', help='Opt-in immutable packed base generation')
    parser.add_argument('--projection', help='Ready persistent SQLite search projection')
    parser.add_argument('--native-executable', help='Explicit helper path for isolated validation')
    args = parser.parse_args()
    if args.packed_index:
        from packed_search_worker import run
        run(args)
        return
    # Keep stdout exclusively for the protocol, including during model loading.
    with contextlib.redirect_stdout(sys.stderr):
        configure_cache_environment()
        import numpy as np
        native_encoder = None
        if args.native_encoder:
            from text_encoder import NativeEncoder
            executable = Path(args.native_executable) if args.native_executable else Path(__file__).resolve().parent.parent/'MacOS/Mami'
            native_encoder = NativeEncoder(executable, args.native_encoder)
        else:
            import torch
            torch.set_num_threads(VISUAL_CPU_THREADS)
            torch.set_num_interop_threads(1)
            from transformers import AutoModel, AutoProcessor
        index = Path(args.index)
        samples = json.loads((index / 'samples.json').read_text())['samples']
        vectors = np.load(index / 'embeddings.npy')
        segments = []
        if args.speech:
            for file in sorted(Path(args.speech).glob('*.json')):
                if file.stem.isdigit():
                    item = json.loads(file.read_text())
                    segments.extend((item['path'], segment) for segment in item.get('transcript', {}).get('segments', []))
        if len(samples) != len(vectors):
            raise ValueError('Embedding and sample counts differ')
        if native_encoder is None:
            device = 'mps' if torch.backends.mps.is_available() else 'cpu'
            path = cached_model_path('visual')
            processor = AutoProcessor.from_pretrained(path, local_files_only=True)
            model = AutoModel.from_pretrained(path, local_files_only=True).to(device).eval()
        catalog = CatalogIndex(samples, vectors, segments, args.catalog)
        catalog.refresh()

    @lru_cache(maxsize=128)
    def encode(query):
        if native_encoder is not None:
            return native_encoder.encode(query)
        with contextlib.redirect_stdout(sys.stderr), torch.inference_mode():
            inputs = processor(text=[query], padding='max_length', truncation=True, return_tensors='pt').to(device)
            feature = model.get_text_features(**inputs)
            if not isinstance(feature, torch.Tensor):
                feature = feature.pooler_output
            return torch.nn.functional.normalize(feature.float(), dim=-1).cpu().numpy()[0]

    # Warm the text encoder before advertising readiness (first MPS call compiles).
    encode('a photo')
    print(json.dumps({'ready': True, 'samples': len(catalog.snapshot.samples)}), flush=True)
    threading.Thread(target=catalog.run, daemon=True).start()
    for line in sys.stdin:
        try:
            started = time.monotonic()
            request = json.loads(line)
            snapshot = catalog.snapshot
            query = request['query'].strip()
            if not query or len(query) > 2000:
                raise ValueError('Query must contain between 1 and 2000 characters')
            allowed = set(request['paths']) if 'paths' in request else None
            mode = request.get('mode', 'both')
            if mode not in ('both', 'visual', 'speech'):
                raise ValueError('Unknown search mode')
            if mode == 'speech':
                hits = snapshot.spoken(query, allowed)
            else:
                hits = snapshot.visual(encode(query), allowed)
                if mode == 'both':
                    hits = combine_hits(hits, snapshot.spoken(query, allowed))
            response = {'hits': hits, 'elapsed': time.monotonic() - started, 'indexed_samples': len(snapshot.samples)}
        except Exception as exc:
            response = {'error': str(exc)}
        print(json.dumps(response, ensure_ascii=False), flush=True)
    catalog.stop.set()
    if native_encoder is not None:
        native_encoder.close()


if __name__ == '__main__':
    main()
