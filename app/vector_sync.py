"""Publish complete packed base generations; retain active readers during cleanup."""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import threading
import time
import uuid
import sys
import tempfile

from packed_vectors import build, FORMAT_VERSION
from vector_overlay import MAX_OVERLAY_ROWS

COMPACTION_INTERVAL_SECONDS = 12 * 60 * 60
MAX_PENDING_ASSETS = 512
RETAINED_GENERATIONS = 3


def resolve_generation(directory):
    directory=Path(directory)
    if (directory/'manifest.json').is_file():
        return directory
    name=json.loads((directory/'current.json').read_text())['generation']
    if Path(name).name!=name or not name.startswith('generation-'):
        raise ValueError('Invalid vector generation pointer')
    return directory/name


def publish(root,generation):
    temporary=root/'current.tmp'
    with temporary.open('w') as output:
        json.dump(dict(generation=generation.name),output)
        output.flush();os.fsync(output.fileno())
    os.replace(temporary,root/'current.json')
    descriptor=os.open(root,os.O_RDONLY)
    try:os.fsync(descriptor)
    finally:os.close(descriptor)


def prune(root):
    owner=json.loads((root/'owner.json').read_text())['id']
    generations=[]
    for path in root.glob('generation-*'):
        if not path.is_dir() or path.is_symlink():continue
        try:manifest=json.loads((path/'manifest.json').read_text())
        except (OSError,ValueError):continue
        if manifest.get('version')==FORMAT_VERSION and manifest.get('owner')==owner:generations.append(path)
    generations.sort(key=lambda path:path.stat().st_mtime,reverse=True)
    current=resolve_generation(root)
    for path in generations[RETAINED_GENERATIONS:]:
        if path==current:continue
        try:
            with (path/'.readers').open('rb') as lease:
                fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
                manifest=json.loads((path/'manifest.json').read_text())
                if manifest.get('version')!=FORMAT_VERSION or manifest.get('owner')!=owner:continue
                shutil.rmtree(path)
        except (BlockingIOError,FileNotFoundError):
            continue


def main(args):
    root=args.output;root.mkdir(parents=True,exist_ok=True)
    stop=threading.Event()
    busy=threading.Event()
    signal.signal(signal.SIGTERM,lambda *_:stop.set())
    signal.signal(signal.SIGINT,lambda *_:stop.set())
    if not args.once:
        def parent():
            try:
                for line in sys.stdin:
                    value=json.loads(line)
                    if value.get('action')=='stop':break
                    if value.get('action')=='editor-activity':
                        busy.set() if value.get('active') else busy.clear()
            finally:stop.set()
        threading.Thread(target=parent,daemon=True).start()
    def checkpoint():
        if stop.is_set():raise InterruptedError('Vector build stopped before publication')
        if busy.is_set():raise InterruptedError('Vector build deferred to active editing')
    with (root/'writer.lock').open('a+b') as owner:
        fcntl.flock(owner,fcntl.LOCK_EX|fcntl.LOCK_NB)
        owner_path=root/'owner.json'
        if not owner_path.exists():
            with owner_path.open('x') as output:
                json.dump(dict(id=uuid.uuid4().hex),output);output.flush();os.fsync(output.fileno())
        owner_id=json.loads(owner_path.read_text())['id']
        # Incomplete workspaces are generated scratch data, never published.
        # Reclaim only workspaces explicitly marked as belonging to this store.
        for workspace in root.glob('.building-*'):
            if workspace.is_symlink() or not workspace.is_dir():continue
            try:marker=json.loads((workspace/'owner.json').read_text())
            except (OSError,ValueError):continue
            if marker.get('id')==owner_id:shutil.rmtree(workspace)
        while not stop.is_set():
            try:
                checkpoint()
                current=resolve_generation(root) if (root/'current.json').exists() else None
                manifest=json.loads((current/'manifest.json').read_text()) if current else None
                with contextlib.closing(sqlite3.connect(args.projection.resolve().as_uri()+'?mode=ro',uri=True)) as db:
                    if manifest:
                        changes=db.execute('SELECT count(DISTINCT asset) FROM vector_events WHERE sequence>?',(manifest['projection_sequence'],)).fetchone()[0]
                        changed_rows=db.execute('SELECT count(*) FROM embeddings WHERE asset IN (SELECT asset FROM vector_events WHERE sequence>?)',(manifest['projection_sequence'],)).fetchone()[0]
                    else:changes=1;changed_rows=0
                if changes and (manifest is None or args.once or changes>=MAX_PENDING_ASSETS or changed_rows>=MAX_OVERLAY_ROWS//2 or time.time()-manifest['created']>=COMPACTION_INTERVAL_SECONDS):
                    generation=root/('generation-'+uuid.uuid4().hex)
                    print(json.dumps(dict(stage='building',generation=str(generation))),flush=True)
                    with tempfile.TemporaryDirectory(prefix='.building-',dir=root) as workspace:
                        with (Path(workspace)/'owner.json').open('x') as output:
                            json.dump(dict(id=owner_id),output);output.flush();os.fsync(output.fileno())
                        staged=Path(workspace)/'generation'
                        build(args.projection,staged,owner=owner_id,checkpoint=checkpoint)
                        checkpoint()
                        os.rename(staged,generation)
                    publish(root,generation)
                    prune(root)
                    print(json.dumps(dict(stage='ready',generation=str(generation))),flush=True)
                if args.once:return
            except InterruptedError as error:
                print(json.dumps(dict(stage='deferred',reason=str(error))),flush=True)
                if args.once:raise
            except Exception as error:
                print(json.dumps(dict(error=str(error))),flush=True)
                if args.once:raise
            stop.wait(60)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--projection',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--once',action='store_true')
    main(parser.parse_args())
