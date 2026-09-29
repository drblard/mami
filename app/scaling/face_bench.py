"""Face-recognition feasibility on a deterministic sample of real originals.

Originals are only read. Decoded frames, crops, embeddings and reports stay in
--run. This measures speed, face yield by resolution and clustering behaviour;
it is not production configuration.
"""
import argparse
import json
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import face_engine  # noqa: E402
from model_config import FACE_MODEL  # noqa: E402
from common import save, progress, lock  # noqa: E402

PHOTO_SUFFIXES = {'.heic', '.jpg', '.jpeg'}
VIDEO_SUFFIXES = {'.mov', '.mp4'}
PREVIEW_SIDE = 640
FULL_SIDES = (1920,)
# Multi-scale configurations: low resolution finds close-ups, high resolution small faces.
MULTI_SCALES = {'multi640+1920': (640, 1920)}
VIDEO_DECODE_SIDE = 1920
MINIMUM_EYE_DISTANCE = 12


def sample(root, photos, videos, seed):
    files = sorted(path for path in root.rglob('*') if path.is_file() and '.mami-imports' not in path.parts)
    pick = random.Random(seed)
    chosen = pick.sample([p for p in files if p.suffix.lower() in PHOTO_SUFFIXES], photos)
    chosen += pick.sample([p for p in files if p.suffix.lower() in VIDEO_SUFFIXES], videos)
    return chosen


def decode_photo(path, scratch):
    target = scratch / 'photo.jpg'
    subprocess.run(['sips', '-s', 'format', 'jpeg', '-s', 'formatOptions', '95', str(path), '--out', str(target)],
                   check=True, capture_output=True, timeout=120)
    with Image.open(target) as image:
        return [(None, ImageOps.exif_transpose(image).convert('RGB'))]


def decode_video(path, scratch):
    frames = scratch / 'frames'
    shutil.rmtree(frames, ignore_errors=True); frames.mkdir()
    side = VIDEO_DECODE_SIDE
    scale = f"scale='if(gt(iw,ih),min({side},iw),-2)':'if(gt(iw,ih),-2,min({side},ih))'"
    subprocess.run(['ffmpeg', '-nostdin', '-loglevel', 'error', '-hwaccel', 'videotoolbox', '-i', str(path),
                    '-vf', 'fps=1,' + scale, '-q:v', '2', str(frames / '%05d.jpg')], check=True, timeout=1800)
    result = []
    for index, frame in enumerate(sorted(frames.glob('*.jpg'))):
        with Image.open(frame) as image:
            result.append((index + 0.5, image.convert('RGB')))
    return result


def analyse(engine, image, sides, crop_source):
    """Detect at `sides`; align crops from `crop_source` (same scene, possibly other resolution)."""
    started = time.perf_counter()
    faces = engine.detect_scales(image, sides)
    detected = time.perf_counter()
    ratio = crop_source.width / image.width
    records, crops = [], []
    for face in faces:
        keypoints = np.asarray(face.keypoints) * ratio
        crop = face_engine.align(crop_source, keypoints)
        crops.append(crop)
        records.append(dict(box=[v * ratio for v in face.box], score=face.score,
                            eye_distance=face_engine.eye_distance(keypoints),
                            sharpness=face_engine.sharpness(crop), frontalness=face_engine.frontalness(keypoints)))
    embeddings, norms = engine.embed(crops)
    for record, norm in zip(records, norms):
        record['norm'] = float(norm)
    return records, crops, embeddings, detected - started, time.perf_counter() - detected


def run(args):
    with lock(args.run, 'faces'):
        run_dir = Path(args.run)
        scratch = run_dir / 'scratch'; scratch.mkdir(exist_ok=True)
        engine = face_engine.FaceEngine(args.models, args.derived, FACE_MODEL, (PREVIEW_SIDE, *FULL_SIDES), accelerate=not args.cpu)
        files = sample(Path(args.root), args.photos, args.videos, args.seed)
        save(run_dir / 'sample.json', [str(p) for p in files])
        configs = {'preview640': None, **MULTI_SCALES}
        store = {name: dict(records=[], embeddings=[]) for name in configs}
        timings = dict(decode=[], video_seconds=0.0, **{name: dict(detect=[], embed=[]) for name in configs})
        (run_dir / 'crops').mkdir(exist_ok=True)
        for number, path in enumerate(files):
            video = path.suffix.lower() in VIDEO_SUFFIXES
            started = time.perf_counter()
            frames = decode_video(path, scratch) if video else decode_photo(path, scratch)
            timings['decode'].append(dict(file=str(path), video=video, frames=len(frames), seconds=time.perf_counter() - started))
            if video:
                timings['video_seconds'] += len(frames)
            for timestamp, image in frames:
                preview = image.copy(); preview.thumbnail((PREVIEW_SIDE, PREVIEW_SIDE), Image.Resampling.LANCZOS)
                for name, side in configs.items():
                    source, detect_side = (preview, (PREVIEW_SIDE,)) if side is None else (image, side)
                    records, crops, embeddings, detect_time, embed_time = analyse(engine, source, detect_side, source)
                    timings[name]['detect'].append(detect_time); timings[name]['embed'].append(embed_time)
                    for record, crop, embedding in zip(records, crops, embeddings):
                        record.update(file=str(path), video=video, timestamp=timestamp, pixels=[image.width, image.height])
                        index = len(store[name]['records'])
                        if name == 'multi640+1920':
                            Image.fromarray(crop).save(run_dir / 'crops' / f'{index:06d}.jpg', quality=90)
                        store[name]['records'].append(record); store[name]['embeddings'].append(embedding)
            if (number + 1) % 25 == 0 or number + 1 == len(files):
                checkpoint(run_dir, store, timings)
            progress(run_dir, 'faces', done=number + 1, total=len(files),
                     faces={name: len(value['records']) for name, value in store.items()})
        save(run_dir / 'timings.json', dict(accelerated=engine.accelerated, **timings))
        shutil.rmtree(scratch, ignore_errors=True)


def checkpoint(run_dir, store, timings):
    """Durable partial results, so an interrupted run keeps its measurements."""
    for name, value in store.items():
        np.save(run_dir / f'{name}-embeddings.npy', np.asarray(value['embeddings'], dtype=np.float32).reshape(-1, face_engine.EMBEDDING_DIMENSIONS))
        save(run_dir / f'{name}-faces.json', value['records'])
    save(run_dir / 'timings-partial.json', timings)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    parser.add_argument('--models', required=True)
    parser.add_argument('--run', required=True)
    parser.add_argument('--photos', type=int, default=150)
    parser.add_argument('--videos', type=int, default=25)
    parser.add_argument('--seed', type=int, default=20260929)
    parser.add_argument('--derived', required=True, help='Generated fixed-shape detectors and CoreML caches')
    parser.add_argument('--cpu', action='store_true')
    run(parser.parse_args())
