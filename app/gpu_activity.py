"""Best-effort Apple GPU load sensing; read-only and no elevated privileges."""
import math
import plistlib
import subprocess
import time
import json
from pathlib import Path
import threading


class EditorActivity:
    """Expiring foreground+recent-input signal, never inferred from GPU load."""
    def __init__(self, log):
        self.log = Path(log)
        self.until = 0
        self.lock = threading.RLock()

    def record(self, event, **details):
        with self.lock:
            # Bounded audit trail: one current and one previous 1 MiB file.
            if self.log.exists() and self.log.stat().st_size >= 1024 * 1024:
                self.log.replace(self.log.with_suffix('.jsonl.1'))
            with self.log.open('a') as output:
                output.write(json.dumps(dict(time=time.time(), event=event, **details)) + '\n')

    def update(self, active):
        with self.lock:
            was_active = self.until > time.monotonic()
            self.until = time.monotonic() + 15 if active else 0
            if active != was_active:
                self.record('editor_active' if active else 'editor_inactive', reason='CapCut foreground with input in last 60 seconds' if active else 'No active editing signal')
            return active != was_active

    def active(self):
        with self.lock:
            if self.until and self.until <= time.monotonic():
                self.until = 0
                self.record('editor_signal_expired')
            return self.until > time.monotonic()


def wait_for_editor(queue):
    activity = queue.editor_activity
    if not activity.active():
        return
    if queue.allow_gpu_defer:
        from index_queue import GPUDeferred
        activity.record('speech_deferred', file=queue.current)
        raise GPUDeferred()
    start = time.monotonic()
    activity.record('speech_wait_started', file=queue.current)
    try:
        while activity.active():
            queue.checkpoint()
            queue.waiting = True
            queue.phase = 'Waiting while CapCut is actively used'
            queue.status()
            queue.wake.wait(.5)
            queue.wake.clear()
    finally:
        activity.record('speech_wait_ended', seconds=round(time.monotonic() - start, 3))


def utilization(tree):
    readings = []
    def visit(value):
        if isinstance(value, dict):
            stats = value.get('PerformanceStatistics', {})
            for key in ('Device Utilization %', 'Renderer Utilization %', 'Tiler Utilization %'):
                reading = stats.get(key)
                if isinstance(reading, (int, float)) and not isinstance(reading, bool) and math.isfinite(reading) and 0 <= reading <= 100:
                    readings.append(float(reading))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(tree)
    return max(readings) if readings else None


def read_utilization():
    try:
        data = subprocess.run(['/usr/sbin/ioreg', '-r', '-c', 'IOAccelerator', '-a'],
                              capture_output=True, check=True, timeout=5).stdout
        return utilization(plistlib.loads(data))
    except (OSError, ValueError, subprocess.SubprocessError, plistlib.InvalidFileException):
        return None


class QuietWindow:
    def __init__(self, threshold=15, seconds=3):
        self.threshold, self.seconds = threshold, seconds
        self.since = self.last = None

    def observe(self, value, now):
        if self.last is not None and now - self.last > 2:
            self.since = None
        self.last = now
        if value is None or value >= self.threshold:
            self.since = None
            return False
        if self.since is None:
            self.since = now
        return now - self.since >= self.seconds


def wait_for_quiet(queue):
    # A fresh quiet window after every inference avoids treating Mami's own
    # previous GPU burst as evidence of an editor export.
    window = QuietWindow()
    while True:
        queue.checkpoint()
        value = read_utilization()
        queue.gpu_utilization = value
        if getattr(queue, 'allow_gpu_defer', False) and (value is None or value >= window.threshold):
            from index_queue import GPUDeferred
            raise GPUDeferred()
        if window.observe(value, time.monotonic()):
            return
        queue.waiting = True
        queue.phase = 'GPU activity unavailable — waiting' if value is None else 'Waiting for GPU to settle'
        queue.status(force=True)
        queue.wake.wait(1)
        queue.wake.clear()
