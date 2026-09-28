"""Isolated real-codec import/preview check, with inference deliberately paused."""
import argparse
import contextlib
import json
import os
from pathlib import Path
import selectors
import sqlite3
import subprocess
import sys
import time


def main(args):
    args.output.mkdir(parents=True, exist_ok=False)
    resources = args.bundle/'Contents/Resources'
    sys.path.insert(0, str(resources))
    from import_media import Importer
    from index_store import connection, ensure_schema

    card, originals = args.output/'card', args.output/'originals'
    card.mkdir(); originals.mkdir()
    database = args.output/'catalog.sqlite'
    with contextlib.closing(sqlite3.connect(database)) as db:
        db.executescript('CREATE TABLE state(id INTEGER PRIMARY KEY,identity TEXT,revision INTEGER,change_token TEXT);'
                         'INSERT INTO state VALUES(1,"pipeline-fixture",0,"initial");'
                         'CREATE TABLE media(path TEXT PRIMARY KEY,asset TEXT,payload TEXT);')
    for index, color in enumerate(('red', 'green', 'blue', 'orange')):
        subprocess.run(['/opt/homebrew/bin/ffmpeg', '-nostdin', '-v', 'error', '-n', '-f', 'lavfi',
                        '-i', f'color=c={color}:s=320x180:r=5', '-t', '3', '-c:v', 'libx264', '-threads', '1',
                        '-metadata', 'creation_time=2026-09-28T12:00:00Z',
                        str(card/f'DJI_20260928120{index}00_0001_D.MP4')], check=True, timeout=30)
    started = time.monotonic()
    publications = []
    def emit(event):
        if event.get('catalog_changed'):
            with contextlib.closing(sqlite3.connect(database)) as db:
                count = db.execute('SELECT count(*) FROM media').fetchone()[0]
            publications.append(dict(seconds=time.monotonic()-started, catalog_count=count))
    importer = Importer(card, originals, 'DJI-Fixture', catalog=database, emit=emit)
    importer.run()
    assert importer.copied == 4 and importer.failed == 0
    assert [row['catalog_count'] for row in publications] == [1, 2, 3, 4]
    with connection(database) as db:
        ensure_schema(db)
        db.execute('UPDATE scan_control SET paused=1 WHERE id=1')
        assert all(not json.loads(row[0])['frames'] for row in db.execute('SELECT payload FROM media'))

    preview_started = time.monotonic()
    thumbnail_seconds = preview_seconds = None
    workers = []
    with contextlib.ExitStack() as stack:
        selector = stack.enter_context(selectors.DefaultSelector())
        try:
            for role in ('preview', 'index'):
                errors = stack.enter_context((args.output/(role+'.log')).open('x'))
                command = [sys.executable, '-B', str(resources/'index_worker.py'), '--database', str(database),
                           '--root', str(originals), '--artifacts', str(args.output/'artifacts'), '--role', role]
                process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
                                           env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
                workers.append(process)
                selector.register(process.stdout, selectors.EVENT_READ, role)
            deadline = time.monotonic()+120
            while time.monotonic() < deadline:
                for key, _ in selector.select(timeout=.2):
                    line = key.fileobj.readline()
                    if not line:
                        raise RuntimeError(f'{key.data} worker exited before completion')
                    event = json.loads(line)
                    if event.get('error'):
                        raise RuntimeError(event['error'])
                with contextlib.closing(sqlite3.connect(database, timeout=10)) as db:
                    media = [json.loads(row[0]) for row in db.execute('SELECT payload FROM media')]
                    if thumbnail_seconds is None and all(item['frames'] for item in media):
                        thumbnail_seconds = time.monotonic()-preview_started
                    if all(item.get('previewState') == 'ready' for item in media):
                        preview_seconds = time.monotonic()-preview_started
                        assert db.execute("SELECT count(*) FROM index_units WHERE stage IN ('embedding','speech')").fetchone()[0] == 0
                        break
            else:
                raise TimeoutError('Preview fixture did not complete')
            rss = subprocess.check_output(['ps', '-o', 'pid,rss,command', '-p', ','.join(str(p.pid) for p in workers)], text=True)
        finally:
            for process in workers:
                process.stdin.close()
            for process in workers:
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait()
    result = dict(status='passed', publications=publications, all_thumbnails_seconds=thumbnail_seconds,
                  all_scrub_previews_seconds=preview_seconds, ai_units=0, source_files_retained=len(list(card.glob('*.MP4'))),
                  workers=rss, caveat='Four short generated clips; codec/process behavior check, not a large-DJI throughput guarantee.')
    with (args.output/'result.json').open('x') as output:
        json.dump(result, output, indent=2)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    main(parser.parse_args())
