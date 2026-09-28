"""Background owner for a durable, rebuildable SQLite search projection."""
import argparse
import fcntl
import json
from pathlib import Path
import signal
import sqlite3
import contextlib
import threading
import time
import sys

from search_store import SearchStore, install_change_log

SYNC_INTERVAL_SECONDS = .25
COMPACTION_INTERVAL_SECONDS = 60


def main(args):
    args.output.parent.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    if not args.once:
        def parent():
            try:
                for line in sys.stdin:
                    if json.loads(line).get('action')=='stop':break
            finally:stop.set()
        threading.Thread(target=parent,daemon=True).start()
    with args.output.with_suffix('.writer.lock').open('a+b') as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        install_change_log(args.catalog)
        with SearchStore(args.output) as store:
            seeded = False
            compacted = time.monotonic()
            source_schema = None
            last_marker = None
            while not stop.is_set():
                try:
                    with contextlib.closing(sqlite3.connect(args.catalog.resolve().as_uri()+'?mode=ro', uri=True)) as source:
                        version = source.execute('PRAGMA schema_version').fetchone()[0]
                    if version != source_schema:
                        install_change_log(args.catalog)
                        with contextlib.closing(sqlite3.connect(args.catalog.resolve().as_uri()+'?mode=ro', uri=True)) as source:
                            source_schema = source.execute('PRAGMA schema_version').fetchone()[0]
                    pending = store.refresh(args.catalog)
                    cursor=store.db.execute('SELECT sequence,seed_after FROM checkpoint WHERE id=1').fetchone()
                    marker=tuple(cursor)
                    if marker!=last_marker:
                        print(json.dumps(dict(changed=True)),flush=True)
                        last_marker=marker
                    if not pending and not seeded:
                        if args.index:
                            store.seed_legacy(args.catalog, args.index, args.speech)
                        seeded = True
                        print(json.dumps(dict(ready=True, projection=str(args.output))), flush=True)
                    if seeded and time.monotonic()-compacted >= COMPACTION_INTERVAL_SECONDS:
                        store.compact_acknowledged_events(args.catalog)
                        if args.vector_root:
                            checkpoint=store.db.execute('SELECT identity,epoch FROM checkpoint WHERE id=1').fetchone()
                            cursors=[]
                            for path in args.vector_root.glob('generation-*/manifest.json'):
                                try:
                                    manifest=json.loads(path.read_text())
                                    if manifest.get('source_identity')==checkpoint['identity'] and manifest.get('epoch')==checkpoint['epoch']:
                                        cursors.append(manifest['projection_sequence'])
                                except (OSError,ValueError,KeyError):
                                    pass
                            if cursors:
                                with store.db:
                                    store.db.execute('DELETE FROM vector_events WHERE sequence<?',(min(cursors),))
                        compacted = time.monotonic()
                    if not pending:
                        if args.once:
                            return
                        stop.wait(SYNC_INTERVAL_SECONDS)
                except Exception as error:
                    print(json.dumps(dict(error=str(error),projection=str(args.output))), flush=True)
                    if args.once:
                        raise
                    # Keep serving the last complete transaction while writes retry.
                    stop.wait(SYNC_INTERVAL_SECONDS)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--index',type=Path)
    parser.add_argument('--speech',type=Path)
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--vector-root',type=Path)
    main(parser.parse_args())
