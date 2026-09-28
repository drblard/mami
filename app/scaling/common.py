"""Durable experiment state; never writes to production data."""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import time


def save(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w') as output:
        json.dump(value, output, indent=2, ensure_ascii=False)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def progress(run, task, **fields):
    value = dict(pid=os.getpid(), task=task, updated=time.time(), **fields)
    save(Path(run) / (task + '-progress.json'), value)
    print(json.dumps(value), flush=True)


@contextlib.contextmanager
def lock(run, task):
    run = Path(run)
    run.mkdir(parents=True, exist_ok=True)
    with (run / (task + '.lock')).open('a+b') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield run


def percentiles(values):
    values = sorted(values)
    return dict(p50_ms=values[len(values)//2], p95_ms=values[min(len(values)-1, int(len(values)*.95))],
                max_ms=max(values), count=len(values))
