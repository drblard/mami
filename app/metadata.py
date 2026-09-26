"""Read capture metadata once at packaging time; never modify media."""
import json
import subprocess
from datetime import datetime
from fractions import Fraction
from pathlib import Path


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


def export_metadata(inventory):
    from PIL import Image
    data = json.loads(Path(inventory).read_text())
    root = Path(data['root'])
    result = {}
    for record in data['files']:
        if 'error' in record:
            continue
        streams = record['probe']['streams']
        stream = next(s for s in streams if s.get('codec_type') == 'video')
        width, height = stream.get('width', 0), stream.get('height', 0)
        metadata = {'date': 'Capture date unavailable', 'details': [], 'location': None,
                    'duration': None, 'tags': [], 'technical': [], 'sortDate': ''}
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
            else:
                probe = subprocess.run(['/opt/homebrew/bin/ffprobe', '-v', 'error', '-show_entries', 'format_tags',
                                        '-of', 'json', str(root / record['path'])], check=True, capture_output=True, text=True, timeout=30)
                tags = json.loads(probe.stdout)['format'].get('tags', {})
                raw_date = tags.get('creation_time') or stream.get('tags', {}).get('creation_time')
                if raw_date:
                    capture = datetime.fromisoformat(raw_date.replace('Z', '+00:00')).astimezone()
                    metadata['date'] = capture.strftime('%d %b %Y · %H:%M')
                    metadata['sortDate'] = capture.strftime('%Y%m%d%H%M%S')
                camera = tags.get('encoder', '')
                camera = camera.replace('OsmoPocket4P', 'Pocket 4P')
                rate = float(Fraction(stream.get('avg_frame_rate', '0/1')))
                resolution = '4K' if max(width, height) == 3840 else f'{width}×{height}'
                metadata['duration'] = float(stream.get('duration') or record['probe']['format']['duration'])
                metadata['details'] = [f'{resolution} · {rate:.0f} fps', camera]
                metadata['technical'] = [tags[k] for k in ('com.dji.camera.LensType', 'com.dji.camera.ColorGammaSxS') if tags.get(k)]
                for key in ('location', 'com.apple.quicktime.location.ISO6709'):
                    if tags.get(key):
                        metadata['location'] = tags[key]
                        break
            metadata['details'] = [v for v in metadata['details'] if v]
        except Exception as exc:
            metadata['error'] = str(exc)
        result[record['path']] = metadata
    return result
