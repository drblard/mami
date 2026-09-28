"""Low-priority JSON-lines controller for the durable background queue."""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import signal
import sys
import threading

os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['OMP_NUM_THREADS'] = '1'
os.environ['OPENBLAS_NUM_THREADS'] = '1'
os.environ['MKL_NUM_THREADS'] = '1'


def main():
    from index_queue import Queue
    from index_backend import Backend
    from gpu_activity import wait_for_editor
    parser = argparse.ArgumentParser()
    parser.add_argument('--database', required=True)
    parser.add_argument('--root', required=True)
    parser.add_argument('--artifacts', required=True)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--role', choices=['all', 'index', 'preview'], default='all')
    args = parser.parse_args()
    os.nice(10)
    protocol = sys.stdout
    output_lock = threading.Lock()
    def emit(event):
        with output_lock:
            print(json.dumps(event, ensure_ascii=False), file=protocol, flush=True)
    with contextlib.ExitStack() as owners:
        try:
            names = ['indexer.lock', 'previewer.lock'] if args.role == 'all' else ['previewer.lock' if args.role == 'preview' else 'indexer.lock']
            for name in names:
                owner = owners.enter_context((Path(args.database).parent / name).open('a+b'))
                fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            emit(dict(phase='Another Mami instance is indexing', done=0, total=0, current='', paused=False, busy=False, changed=False, error='Close the other Mami instance before restarting this worker.'))
            return
        with contextlib.redirect_stdout(sys.stderr):
            queue = Queue(args.database, args.root, args.artifacts, Backend(args.artifacts), emit, gpu_wait=wait_for_editor, role=args.role)
            queue.editor_activity.record('worker_started', role=args.role,
                                         policy='CapCut foreground + input within 60s; no global GPU gate')
            signal.signal(signal.SIGTERM, lambda *_: (queue.stop.set(), queue.wake.set()))
            def controls():
                try:
                    for line in sys.stdin:
                        queue.command(json.loads(line))
                except Exception as error:
                    queue.status(error=str(error))
                finally:
                    queue.stop.set()
                    queue.wake.set()
            if args.once:
                if args.role != 'preview':
                    queue.scan()
                queue.work()
            else:
                threading.Thread(target=controls, daemon=True).start()
                queue.run()


if __name__ == '__main__':
    main()
