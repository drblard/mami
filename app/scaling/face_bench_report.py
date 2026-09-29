"""Summarize a face_bench run: yield, measured cost, projected library time, group sheets."""
import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import face_people  # noqa: E402

CONFIG = 'multi640+1920'
SHEET_GROUPS = 24
SHEET_FACES = 14
CROP = 72


def report(run, photos, video_seconds):
    run = Path(run)
    timings = json.loads((run / 'timings.json').read_text())
    decode = timings['decode']
    photo_decode = [d['seconds'] for d in decode if not d['video']]
    video_decode = sum(d['seconds'] for d in decode if d['video'])
    frames = sum(d['frames'] for d in decode if d['video'])
    photo_count = len(photo_decode)
    result = dict(sample=dict(photos=photo_count, videos=sum(d['video'] for d in decode), video_seconds=timings['video_seconds']),
                  accelerated=timings['accelerated'], configs={})
    for name in ('preview640', 'multi640+1920'):
        faces = json.loads((run / f'{name}-faces.json').read_text())
        detect, embed = timings[name]['detect'], timings[name]['embed']
        # Detection/embedding samples are one per photo, then one per video frame, in order.
        photo_cost = sum(detect[:photo_count]) + sum(embed[:photo_count])
        video_cost = sum(detect[photo_count:]) + sum(embed[photo_count:])
        reliable = [f for f in faces if face_people.Quality(f['score'], f['eye_distance'], f['frontalness']).reliable]
        per_photo = (sum(photo_decode) + photo_cost) / max(1, photo_count)
        per_video_second = (video_decode + video_cost) / max(1, frames)
        result['configs'][name] = dict(
            faces=len(faces), photo_faces=sum(not f['video'] for f in faces), video_faces=sum(f['video'] for f in faces),
            reliable=len(reliable), detect_median_ms=round(statistics.median(detect) * 1000, 1),
            seconds_per_photo=round(per_photo, 3), seconds_per_video_second=round(per_video_second, 3),
            projected_library_hours=round((photos * per_photo + video_seconds * per_video_second) / 3600, 2))
    return result


def groups(run):
    run = Path(run)
    faces = json.loads((run / f'{CONFIG}-faces.json').read_text())
    embeddings = np.load(run / f'{CONFIG}-embeddings.npy')
    # Mirror production: tracks per video, best representatives, reliable faces only.
    chosen = []
    by_file = defaultdict(list)
    for index, face in enumerate(faces):
        by_file[face['file']].append(index)
    for members in by_file.values():
        qualities = [face_people.Quality(faces[i]['score'], faces[i]['eye_distance'], faces[i]['frontalness']) for i in members]
        if faces[members[0]]['video']:
            labels = face_people.tracks(embeddings[members], [faces[i]['timestamp'] for i in members])
            keep = face_people.representatives(labels, qualities)
        else:
            keep = range(len(members))
        chosen += [members[k] for k in keep if qualities[k].reliable]
    labels = face_people.groups(embeddings[chosen])
    clusters = defaultdict(list)
    for position, label in enumerate(labels):
        clusters[label].append(chosen[position])
    ordered = sorted(clusters.values(), key=len, reverse=True)
    return chosen, ordered


def sheet(run, clusters, output):
    rows = [cluster for cluster in clusters if len(cluster) >= 2][:SHEET_GROUPS]
    image = Image.new('RGB', (40 + SHEET_FACES * (CROP + 4), max(1, len(rows)) * (CROP + 6)), (20, 20, 24))
    draw = ImageDraw.Draw(image)
    for row, cluster in enumerate(rows):
        y = row * (CROP + 6)
        draw.text((4, y + CROP // 2 - 6), str(len(cluster)), fill=(220, 220, 220))
        for column, index in enumerate(cluster[:SHEET_FACES]):
            with Image.open(Path(run) / 'crops' / f'{index:06d}.jpg') as crop:
                image.paste(crop.resize((CROP, CROP)), (40 + column * (CROP + 4), y))
    image.save(output, quality=85)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True)
    parser.add_argument('--library-photos', type=int, required=True)
    parser.add_argument('--library-video-seconds', type=float, required=True)
    args = parser.parse_args()
    summary = report(args.run, args.library_photos, args.library_video_seconds)
    chosen, clusters = groups(args.run)
    summary['grouping'] = dict(config=CONFIG, reliable_representatives=len(chosen),
                               groups_of_two_or_more=sum(len(c) >= 2 for c in clusters),
                               grouped_faces=sum(len(c) for c in clusters if len(c) >= 2),
                               largest=[len(c) for c in clusters[:10]])
    sheet(args.run, clusters, Path(args.run) / 'groups.jpg')
    (Path(args.run) / 'report.json').write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
