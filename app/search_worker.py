"""Persistent, local JSON-lines search worker for the native prototype."""
import argparse
import contextlib
import json
import sys
import time
import re
import unicodedata
import sqlite3
from pathlib import Path


def words(text):
    folded = ''.join(c for c in unicodedata.normalize('NFD', text.casefold())
                     if not unicodedata.combining(c))
    return re.findall(r'\w+', folded)


def speech_hits(query, segments, by_path, allowed=None):
    terms = set(words(query))
    hits = []
    if not terms:
        return hits
    for path, segment in segments:
        if allowed is not None and path not in allowed:
            continue
        text = segment['text'].strip()
        if terms.issubset(set(words(text))) and path in by_path:
            timestamp = float(segment['start'])
            frame = min(by_path[path], key=lambda s: abs((s['timestamp'] or 0) - timestamp))
            hits.append({**frame, 'timestamp': timestamp, 'evidence': text,
                         'score': len(terms) / max(1, len(words(text)))})
    hits.sort(key=lambda hit: (-hit['score'], hit['path'], hit['timestamp']))
    result, seen = [], set()
    for hit in hits:
        if hit['path'] not in seen:
            result.append(hit)
            seen.add(hit['path'])
        if len(result) == 60:
            break
    return result


def combine_hits(visual, spoken, limit=60):
    """Reciprocal-rank fusion avoids comparing unrelated model/lexical scores.

For a file matched by speech, open the actual spoken segment and retain its
excerpt. Agreement between searches boosts the file, not a claimed same moment.
"""
    merged, scores = {}, {}
    for results in (visual, spoken):
        for rank, hit in enumerate(results, start=1):
            path = hit['path']
            scores[path] = scores.get(path, 0) + 1 / (60 + rank)
            if path not in merged or hit.get('evidence'):
                merged[path] = hit
    order = sorted(scores, key=lambda path: (-scores[path], path))
    return [{**merged[path], 'score': scores[path]} for path in order[:limit]]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--index', required=True)
    parser.add_argument('--speech')
    parser.add_argument('--catalog')
    args = parser.parse_args()
    # Keep stdout exclusively for the protocol, including during model loading.
    with contextlib.redirect_stdout(sys.stderr):
        from lab import LAB, MODELS
        import numpy as np
        import torch
        from transformers import AutoModel, AutoProcessor
        index = Path(args.index)
        samples = json.loads((index / 'samples.json').read_text())['samples']
        vectors = np.load(index / 'embeddings.npy')
        by_path = {}
        for sample in samples:
            by_path.setdefault(sample['path'], []).append(sample)
        segments = []
        if args.speech:
            for file in sorted(Path(args.speech).glob('*.json')):
                if file.stem.isdigit():
                    item = json.loads(file.read_text())
                    segments.extend((item['path'], segment) for segment in item.get('transcript', {}).get('segments', []))
        if len(samples) != len(vectors):
            raise ValueError('Embedding and sample counts differ')
        device = 'mps' if torch.backends.mps.is_available() else 'cpu'
        from huggingface_hub import snapshot_download
        repo, revision = MODELS['visual']
        path = snapshot_download(repo, revision=revision, local_files_only=True,
                                 cache_dir=LAB / 'cache/huggingface/hub',
                                 allow_patterns=['*.json', '*.safetensors', '*.npz', '*.bin', '*.model', '*.txt', '*.jinja'])
        processor = AutoProcessor.from_pretrained(path, local_files_only=True)
        model = AutoModel.from_pretrained(path, local_files_only=True).to(device).eval()
    print(json.dumps({'ready': True, 'samples': len(samples)}), flush=True)
    base_samples, base_vectors, base_segments = samples, vectors, list(segments)
    catalog_token = None
    def refresh_catalog():
        nonlocal samples, vectors, segments, by_path, catalog_token
        if not args.catalog:
            return
        with contextlib.closing(sqlite3.connect(Path(args.catalog).resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
            db.execute('BEGIN')
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='index_units'").fetchone():
                return
            token = db.execute('SELECT change_token FROM state').fetchone()[0]
            if token == catalog_token:
                return
            from index_queue import PIPELINE
            # Only serve results whose asset is still in the media catalog.
            rows = db.execute("SELECT stage,payload FROM index_units WHERE pipeline=? AND stage IN ('embedding','speech') AND asset IN (SELECT asset FROM media) ORDER BY asset,ordinal", (PIPELINE,)).fetchall()
        extra_samples, extra_vectors, extra_segments = [], [], []
        for stage, payload in rows:
            value = json.loads(payload)
            if stage == 'embedding':
                if Path(value['vector']).is_file() and Path(value['sample']['frame']).is_file():
                    extra_vectors.append(np.load(value['vector'], allow_pickle=False))
                    extra_samples.append(value['sample'])
            else:
                extra_segments.extend((value['path'], segment) for segment in value['segments'])
        samples = base_samples + extra_samples
        vectors = np.concatenate([base_vectors, np.stack(extra_vectors)]) if extra_vectors else base_vectors
        segments = base_segments + extra_segments
        by_path = {}
        for sample in samples:
            by_path.setdefault(sample['path'], []).append(sample)
        catalog_token = token
    for line in sys.stdin:
        try:
            request = json.loads(line)
            refresh_catalog()
            query = request['query'].strip()
            if not query or len(query) > 2000:
                raise ValueError('Query must contain between 1 and 2000 characters')
            started = time.monotonic()
            allowed = set(request['paths']) if 'paths' in request else None
            mode = request.get('mode', 'both')
            if mode not in ('both', 'visual', 'speech'):
                raise ValueError('Unknown search mode')
            if mode == 'speech':
                hits = speech_hits(query, segments, by_path, allowed)
                print(json.dumps({'hits': hits, 'elapsed': time.monotonic() - started, 'indexed_samples': len(samples)}, ensure_ascii=False), flush=True)
                continue
            with contextlib.redirect_stdout(sys.stderr), torch.inference_mode():
                inputs = processor(text=[query], padding='max_length', truncation=True, return_tensors='pt').to(device)
                feature = model.get_text_features(**inputs)
                if not isinstance(feature, torch.Tensor):
                    feature = feature.pooler_output
                feature = torch.nn.functional.normalize(feature.float(), dim=-1).cpu().numpy()[0]
                scores = vectors @ feature
            hits, seen = [], set()
            for i in np.argsort(-scores):
                sample = samples[int(i)]
                if sample['path'] in seen or (allowed is not None and sample['path'] not in allowed):
                    continue
                seen.add(sample['path'])
                hits.append({**sample, 'score': float(scores[i])})
                if len(hits) >= 60:
                    break
            if mode == 'both':
                hits = combine_hits(hits, speech_hits(query, segments, by_path, allowed))
            response = {'hits': hits, 'elapsed': time.monotonic() - started, 'indexed_samples': len(samples)}
        except Exception as exc:
            response = {'error': str(exc)}
        print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
