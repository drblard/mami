"""Repeat real DJI imports into an isolated library; never remove source files."""
import argparse
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time


def main(args):
    args.output.mkdir(parents=True, exist_ok=False)
    resources = args.bundle / 'Contents/Resources'
    sys.path.insert(0, str(resources))
    from import_media import Importer
    from index_store import connection, ensure_schema, signature

    sources = sorted(args.source.glob('*.MP4'))
    if not sources:
        raise ValueError('No DJI clips in the specified source directory')
    before = {str(path): signature(path) for path in sources}
    database = args.output / 'catalog.sqlite'
    originals = args.output / 'originals'
    originals.mkdir()
    with contextlib.closing(sqlite3.connect(database)) as db:
        db.executescript('CREATE TABLE state(id INTEGER PRIMARY KEY,identity TEXT,revision INTEGER,change_token TEXT);'
                         'INSERT INTO state VALUES(1,"dji-timing",0,"initial");'
                         'CREATE TABLE media(path TEXT PRIMARY KEY,asset TEXT,payload TEXT);')
    with connection(database) as db:
        ensure_schema(db)
        db.execute('UPDATE scan_control SET paused=1 WHERE id=1')
    start = time.monotonic()
    results, errors = {}, []
    result_lock = threading.Lock()
    def rows():
        with contextlib.closing(sqlite3.connect(database, timeout=10)) as db:
            return [(path, json.loads(payload)) for path, payload in db.execute('SELECT path,payload FROM media')]
    def progress(event):
        if event.get('catalog_changed'):
            with result_lock:
                for path, media in rows():
                    results.setdefault(path, dict(filename=Path(path).name, catalogued_seconds=time.monotonic()-start,
                                                 playable_path=path, initial_frames=len(media['frames'])))
    policies = {str(path.relative_to(args.source)): 'skip' for path in args.source.rglob('*')
                if path.is_file() and path not in sources}
    importer = Importer(args.source, originals, 'DJI-Timing', catalog=database, emit=progress,
                        remove_source=False, eject_after=False, policy_json=json.dumps(policies))
    def importing():
        try:
            importer.run()
        except Exception as error:
            errors.append(str(error))
    environment = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
    workers = []
    with contextlib.ExitStack() as stack:
        try:
            for role in ('preview', 'index'):
                log = stack.enter_context((args.output / (role+'.log')).open('x'))
                worker = subprocess.Popen([sys.executable, '-B', str(resources/'index_worker.py'),
                                           '--database', str(database), '--root', str(originals),
                                           '--artifacts', str(args.output/'artifacts'), '--role', role],
                                          stdin=subprocess.PIPE, stdout=log, stderr=log, env=environment)
                workers.append(worker)
            thread = threading.Thread(target=importing)
            thread.start()
            deadline = time.monotonic() + args.timeout
            while time.monotonic() < deadline:
                if errors or any(worker.poll() is not None for worker in workers):
                    raise RuntimeError(str(errors) if errors else 'Worker exited early')
                current = rows()
                with result_lock:
                    for path, media in current:
                        record = results.setdefault(path, dict(filename=Path(path).name, catalogued_seconds=time.monotonic()-start,
                                                               playable_path=path, initial_frames=0))
                        delay = time.monotonic()-start-record['catalogued_seconds']
                        if media['frames']:
                            record.setdefault('thumbnail_after_catalog_seconds', delay)
                        duration = (media.get('metadata') or {}).get('duration')
                        times = [frame['timestamp'] for frame in media['frames'] if frame['timestamp'] is not None]
                        if duration and times and max(times) >= duration*.9:
                            record.setdefault('full_range_scrub_after_catalog_seconds', delay)
                        if media.get('previewState') == 'ready':
                            record.setdefault('dense_preview_after_catalog_seconds', delay)
                            record['frames'] = len(media['frames'])
                            record['duration'] = duration
                    temporary = args.output/'progress.tmp'
                    temporary.write_text(json.dumps(dict(pid=os.getpid(),files=list(results.values())), indent=2))
                    os.replace(temporary, args.output/'progress.json')
                if not thread.is_alive() and len(current) == len(sources) and all(item.get('previewState') == 'ready' for _,item in current):
                    break
                time.sleep(.1)
            else:
                raise TimeoutError('DJI responsiveness check exceeded its deadline')
            thread.join()
            if importer.failed:
                raise RuntimeError(f'{importer.failed} imports failed')
            with contextlib.closing(sqlite3.connect(database)) as db:
                ai_units = db.execute("SELECT count(*) FROM index_units WHERE stage IN ('embedding','speech')").fetchone()[0]
            if ai_units:
                raise RuntimeError('AI ran while its queue was paused')
        finally:
            importer.stop.set(); importer.wake.set()
            if 'thread' in locals():
                thread.join(timeout=65)
            for worker in workers:
                worker.stdin.close()
            for worker in workers:
                try:
                    worker.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    worker.kill(); worker.wait()
    if any(signature(Path(path)) != value for path,value in before.items()):
        raise RuntimeError('Source file signature changed during the test')
    result = dict(status='passed', files=list(results.values()), elapsed_seconds=time.monotonic()-start,
                  copied=importer.copied, ai_units=ai_units, source_signatures_unchanged=True,
                  caveat='Real DJI footage copied SSD-to-SSD, not USB. Catalog observation excludes native grid rendering; playback is measured separately.')
    (args.output/'result.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--timeout', type=float, default=900)
    main(parser.parse_args())
