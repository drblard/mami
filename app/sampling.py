"""Deterministic frame sampling shared by production preview and inference work."""
from fractions import Fraction
import math

UNKNOWN_FRAME_DURATION_SECONDS = 0.1
END_SEEK_MARGIN_SECONDS = 0.001


def sample_times(length, interval):
    if not all(math.isfinite(value) and value > 0 for value in (length, interval)):
        raise ValueError('Video duration and sampling interval must be positive and finite')
    return [(start + min(start + interval, length)) / 2
            for start in (i * interval for i in range(math.ceil(length / interval)))]


def frame_rate(stream):
    try:
        rate = float(Fraction(stream.get('avg_frame_rate') or '0/1'))
        return rate if math.isfinite(rate) and rate > 0 else 0
    except (ValueError, TypeError, ZeroDivisionError):
        return 0


def last_frame_time(probe):
    stream = next((stream for stream in probe['streams'] if stream.get('codec_type') == 'video'), None)
    if stream is None:
        raise ValueError('No video stream found')
    length = float(stream.get('duration') or probe.get('format', {}).get('duration', 0))
    if not math.isfinite(length) or length <= 0:
        raise ValueError('Video duration must be positive and finite')
    rate = frame_rate(stream)
    frame_duration = 1 / rate if rate else UNKNOWN_FRAME_DURATION_SECONDS
    return max(0, length - frame_duration - END_SEEK_MARGIN_SECONDS)
