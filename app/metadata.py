"""Capture metadata extraction; accepts existing probes and never modifies media."""
import json
from datetime import datetime
from pathlib import Path
from sampling import frame_rate


def gps_label(gps):
    # DJI writes placeholder zero coordinates with GPSStatus V (invalid).
    if gps.get(9) == 'V' or not all(k in gps for k in (1, 2, 3, 4)):
        return None
    try:
        lat, lon = [sum(float(v) / scale for v, scale in zip(gps[k], (1, 60, 3600))) for k in (2, 4)]
        if gps[1] not in ('N', 'S') or gps[3] not in ('E', 'W'):
            return None
        if not (0 <= lat <= 90 and 0 <= lon <= 180) or (lat == 0 and lon == 0):
            return None
        return f"{lat:.3f}°{gps[1]} {lon:.3f}°{gps[3]}"
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def empty_metadata():
    return {'date': 'Capture date unavailable', 'details': [], 'location': None,
            'duration': None, 'tags': [], 'technical': [], 'sortDate': ''}


def video_metadata(probe, timezone=None):
    """Extract video fields from one ffprobe result, without further I/O."""
    metadata = empty_metadata()
    try:
        stream = next((item for item in probe['streams'] if item.get('codec_type') == 'video'), None)
        if stream is None:
            raise ValueError('No video stream found')
        width, height = stream.get('width', 0), stream.get('height', 0)
        tags = probe.get('format', {}).get('tags', {})
        raw_date = tags.get('creation_time') or stream.get('tags', {}).get('creation_time')
        if raw_date:
            capture = datetime.fromisoformat(raw_date.replace('Z', '+00:00')).astimezone(timezone)
            metadata['date'] = capture.strftime('%d %b %Y · %H:%M')
            metadata['sortDate'] = capture.strftime('%Y%m%d%H%M%S')
        camera = ' '.join(tags.get('com.apple.quicktime.' + key, '').strip() for key in ('make', 'model')).strip()
        if not camera:
            camera = ' '.join(tags.get(key, '').strip() for key in ('make', 'model')).strip()
        if not camera and any(name in tags.get('encoder', '') for name in ('DJI', 'OsmoPocket')):
            camera = tags['encoder']
        camera = camera.replace('OsmoPocket4P', 'Pocket 4P')
        metadata['camera'] = camera
        rate = frame_rate(stream)
        resolution = '4K' if max(width, height) == 3840 else f'{width}×{height}'
        metadata['duration'] = float(stream.get('duration') or probe['format']['duration'])
        metadata['details'] = [f'{resolution} · {rate:.0f} fps' if rate else resolution]
        if camera:
            metadata['details'].append(camera)
        metadata['technical'] = [tags[key] for key in ('com.dji.camera.LensType', 'com.dji.camera.ColorGammaSxS') if tags.get(key)]
        for key in ('location', 'com.apple.quicktime.location.ISO6709'):
            if tags.get(key):
                metadata['location'] = tags[key]
                break
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        metadata['error'] = str(error)
    return metadata


def export_metadata(inventory):
    from PIL import Image
    data = json.loads(Path(inventory).read_text())
    root = Path(data['root'])
    result = {}
    for record in data['files']:
        if 'error' in record:
            continue
        if record['kind'] != 'image':
            result[record['path']] = video_metadata(record['probe'])
            continue
        streams = record['probe']['streams']
        stream = next(s for s in streams if s.get('codec_type') == 'video')
        width, height = stream.get('width', 0), stream.get('height', 0)
        metadata = empty_metadata()
        try:
            if record['kind'] == 'image':
                with Image.open(root / record['path']) as image:
                    exif = image.getexif()
                    details = exif.get_ifd(34665)
                    raw_date = details.get(36867) or exif.get(306)
                    if raw_date:
                        capture = datetime.strptime(str(raw_date), '%Y:%m:%d %H:%M:%S')
                        metadata['date'] = capture.strftime('%d %b %Y · %H:%M')
                        metadata['sortDate'] = capture.strftime('%Y%m%d%H%M%S')
                    camera = ' '.join(str(exif.get(k, '')).strip() for k in (271, 272)).strip()
                    metadata['location'] = gps_label(exif.get_ifd(34853))
                    keywords = exif.get(40094, b'')
                    if isinstance(keywords, bytes):
                        keywords = keywords.decode('utf-16-le', errors='replace').rstrip('\0')
                    metadata['tags'] = [t.strip() for t in str(keywords).split(';') if t.strip()]
                    for key, label in [(34855, 'ISO'), (33437, 'ƒ/'), (41989, 'mm equivalent')]:
                        if key in details:
                            metadata['technical'].append(f'{label} {details[key]}')
                metadata['details'] = [f'{width * height / 1e6:.1f} MP', camera]
            metadata['details'] = [v for v in metadata['details'] if v]
        except Exception as exc:
            metadata['error'] = str(exc)
        result[record['path']] = metadata
    return result
