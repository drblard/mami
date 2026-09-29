"""Extract faces from one original: decode, detect, embed, track and crop."""
import json
import subprocess
import tempfile
from pathlib import Path

import numpy as np

import face_engine
import face_people

# Close-ups are found at low resolution and small faces at high resolution.
DETECTION_SIDES = (640, 1920)
VIDEO_FRAME_SIDE = 1920
VIDEO_FRAME_INTERVAL_SECONDS = 1.0
VIDEO_FIRST_FRAME_SECONDS = 0.5
THUMBNAIL_SIDE = 160
THUMBNAIL_MARGIN = 0.35
DECODE_TIMEOUT_SECONDS = 1800


def display_size(stream):
    """Width/height after the rotation ffmpeg applies automatically."""
    width, height = int(stream['width']), int(stream['height'])
    rotation = 0
    for data in stream.get('side_data_list', []):
        if 'rotation' in data:
            rotation = int(float(data['rotation']))
    rotation = int(float(stream.get('tags', {}).get('rotate', rotation)))
    return (height, width) if abs(rotation) % 180 == 90 else (width, height)


def scaled_size(width, height, side=VIDEO_FRAME_SIDE):
    """Even output dimensions no larger than side, preserving aspect."""
    scale = min(1.0, side / max(width, height))
    even = lambda value: max(2, int(round(value * scale / 2)) * 2)
    return even(width), even(height)


def normalized_box(box, width, height):
    x1, y1, x2, y2 = box
    clamp = lambda value: min(1.0, max(0.0, value))
    return (clamp(x1 / width), clamp(y1 / height), clamp(x2 / width), clamp(y2 / height))


def thumbnail(image, box):
    """Square face thumbnail with context, for review screens."""
    from PIL import Image
    x1, y1, x2, y2 = box
    size = max(x2 - x1, y2 - y1) * (1 + 2 * THUMBNAIL_MARGIN)
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    region = (int(cx - size / 2), int(cy - size / 2), int(cx + size / 2), int(cy + size / 2))
    return image.crop(region).resize((THUMBNAIL_SIDE, THUMBNAIL_SIDE), Image.Resampling.LANCZOS)


class FaceExtractor:
    def __init__(self, engine, image_helper, ffmpeg, ffprobe, crops, run=subprocess.run):
        self.engine, self.image_helper, self.ffmpeg, self.ffprobe = engine, image_helper, ffmpeg, ffprobe
        self.crops, self.run = Path(crops), run

    def analyse(self, image, timestamp):
        faces = self.engine.detect_scales(image, DETECTION_SIDES)
        crops = [face_engine.align(image, face.keypoints) for face in faces]
        embeddings, norms = self.engine.embed(crops)
        results = []
        for face, crop, embedding, norm in zip(faces, crops, embeddings, norms):
            quality = face_people.Quality(face.score, face_engine.eye_distance(face.keypoints), face_engine.frontalness(face.keypoints))
            results.append(dict(timestamp=timestamp, box=normalized_box(face.box, image.width, image.height),
                                score=face.score, eye_distance=quality.eye_distance, frontalness=quality.frontalness,
                                sharpness=face_engine.sharpness(crop), norm=float(norm), reliable=quality.reliable,
                                quality=quality, embedding=embedding, thumbnail=thumbnail(image, face.box)))
        return results

    def photo(self, path, cancelled, heartbeat):
        from PIL import Image
        with tempfile.TemporaryDirectory(prefix='mami-faces-') as directory:
            target = Path(directory) / 'frame.jpg'
            self.run([str(self.image_helper), '--image-face-frame', str(path), str(target)],
                     check=True, capture_output=True, timeout=120)
            heartbeat()
            with Image.open(target) as image:
                faces = self.analyse(image.convert('RGB'), None)
            heartbeat()
        for index, face in enumerate(faces):
            face.update(track=index, representative=True)
        return faces

    def video_frames(self, path):
        from PIL import Image
        probe = json.loads(self.run([self.ffprobe, '-v', 'error', '-select_streams', 'v:0', '-show_streams', '-of', 'json', str(path)],
                                    check=True, capture_output=True, text=True, timeout=60).stdout)
        streams = probe.get('streams') or []
        if not streams:
            raise ValueError('No video stream found')
        width, height = scaled_size(*display_size(streams[0]))
        command = [self.ffmpeg, '-nostdin', '-v', 'error', '-hwaccel', 'videotoolbox', '-ss', str(VIDEO_FIRST_FRAME_SECONDS),
                   '-i', str(path), '-vf', f'fps={1 / VIDEO_FRAME_INTERVAL_SECONDS},scale={width}:{height}',
                   '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-']
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        size = width * height * 3
        index = 0
        try:
            while True:
                data = process.stdout.read(size)
                if not data:
                    break
                if len(data) != size:
                    raise RuntimeError('ffmpeg returned a partial video frame')
                yield VIDEO_FIRST_FRAME_SECONDS + index * VIDEO_FRAME_INTERVAL_SECONDS, Image.frombytes('RGB', (width, height), data)
                index += 1
        finally:
            if process.poll() is None:
                process.kill()
            process.stdout.close()
            error = process.stderr.read().decode(errors='replace').strip()
            process.stderr.close()
            status = process.wait(timeout=DECODE_TIMEOUT_SECONDS)
        if status != 0:
            raise RuntimeError('ffmpeg: ' + (error[-1000:] or f'exit status {status}'))

    def video(self, path, cancelled, heartbeat):
        faces = []
        for timestamp, image in self.video_frames(path):
            if cancelled():
                raise InterruptedError('Face extraction cancelled')
            faces.extend(self.analyse(image, timestamp))
            heartbeat()
        if not faces:
            return []
        labels = face_people.tracks(np.stack([face['embedding'] for face in faces]), [face['timestamp'] for face in faces])
        chosen = set(face_people.representatives(labels, [face['quality'] for face in faces]))
        for index, (face, label) in enumerate(zip(faces, labels)):
            face.update(track=label, representative=index in chosen)
        return faces

    def extract(self, asset, path, kind, cancelled=lambda: False, heartbeat=lambda: None):
        """heartbeat() is called after every analysed frame; silence means a stall."""
        faces = (self.photo if kind == 'image' else self.video)(path, cancelled, heartbeat)
        directory = self.crops / asset.replace(':', '-')
        # Thumbnails are derived; a re-extraction replaces the whole set.
        for stale in directory.glob('*.jpg') if directory.exists() else ():
            stale.unlink()
        for index, face in enumerate(faces):
            image = face.pop('thumbnail')
            face.pop('quality')
            if face['representative']:
                directory.mkdir(parents=True, exist_ok=True)
                target = directory / f'{index:05d}.jpg'
                partial = target.with_suffix('.partial')
                image.save(partial, format='JPEG', quality=88)
                partial.replace(target)
                face['crop'] = str(target)
        return faces
