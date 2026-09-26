"""Best-effort Apple GPU load sensing; read-only and no elevated privileges."""
import math
import plistlib
import subprocess
import time


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
        if window.observe(value, time.monotonic()):
            return
        queue.waiting = True
        queue.phase = 'GPU activity unavailable — waiting' if value is None else 'Waiting for GPU to settle'
        queue.status(force=True)
        queue.wake.wait(1)
        queue.wake.clear()
