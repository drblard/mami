"""Low-priority atlas backfill for completed AI jobs; originals are never opened."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import sys
import threading

from index_store import connection
from model_config import PIPELINE
from preview_atlas import ATLAS_VERSION,ensure_pack_schema,pack_asset,retire_raw_frames

MAX_PACK_ATTEMPTS = 3
CACHE_POLL_SECONDS = 30
RETRY_DELAY_SECONDS = 5


def check_backfill_priority(database, stop, busy):
    if stop.is_set():raise InterruptedError('Preview packing stopped')
    if busy.is_set():raise InterruptedError('Preview packing yields to active editing')
    with connection(database) as db:
        if db.execute("SELECT 1 FROM preview_jobs WHERE state IN ('queued','running') LIMIT 1").fetchone():
            raise InterruptedError('Preview packing yields to pending previews')


def main(args):
    os.nice(10)
    stop,busy=threading.Event(),threading.Event()
    signal.signal(signal.SIGTERM,lambda *_:stop.set())
    def checkpoint():
        check_backfill_priority(args.database,stop,busy)
    def controls():
        try:
            for line in sys.stdin:
                value=json.loads(line)
                if value.get('action')=='stop':break
                if value.get('action')=='editor-activity':busy.set() if value.get('active') else busy.clear()
        finally:stop.set()
    if not args.once:threading.Thread(target=controls,daemon=True).start()
    with (args.database.parent/'preview-cache.lock').open('a+b') as owner:
        fcntl.flock(owner,fcntl.LOCK_EX|fcntl.LOCK_NB)
        ensure_pack_schema(args.database)
        while not stop.is_set():
            asset=None
            try:
                checkpoint()
                with connection(args.database) as db:
                    preview_pending=bool(db.execute("SELECT 1 FROM preview_jobs WHERE state IN ('queued','running') LIMIT 1").fetchone())
                    if preview_pending:
                        asset=None
                    else:
                        row=db.execute("SELECT j.asset FROM index_jobs j WHERE j.state='complete' AND j.kind='video' AND NOT EXISTS (SELECT 1 FROM preview_pack_errors e WHERE e.asset=j.asset AND e.attempts>=?) AND NOT EXISTS (SELECT 1 FROM preview_packs p WHERE p.asset=j.asset AND p.version=?) AND EXISTS (SELECT 1 FROM index_units u WHERE u.asset=j.asset AND u.pipeline=? AND u.stage='frame' AND u.ordinal=1) ORDER BY j.capture_time DESC LIMIT 1",(MAX_PACK_ATTEMPTS,ATLAS_VERSION,PIPELINE)).fetchone()
                        asset=row['asset'] if row else None
                if asset is None:
                    if args.projection and not preview_pending:
                        with connection(args.database) as db:
                            retired_candidates=db.execute('SELECT p.asset,p.directory FROM preview_packs p WHERE NOT EXISTS (SELECT 1 FROM preview_retired r WHERE r.asset=p.asset AND r.directory=p.directory) AND NOT EXISTS (SELECT 1 FROM preview_pack_errors e WHERE e.asset=p.asset AND e.attempts>=?) LIMIT 16',(MAX_PACK_ATTEMPTS,)).fetchall()
                        made_progress=False
                        for candidate in retired_candidates:
                            checkpoint()
                            asset=candidate['asset']
                            removed=retire_raw_frames(args.database,asset,args.artifacts,args.projection)
                            if removed is None:continue
                            made_progress=True
                            if removed==0:
                                with connection(args.database) as db:
                                    db.execute('INSERT OR REPLACE INTO preview_retired VALUES(?,?)',(asset,candidate['directory']))
                            else:print(json.dumps(dict(changed=True,stage='ready',retired_raw_frames=removed)),flush=True)
                        if made_progress:continue
                    if args.once:return
                    stop.wait(CACHE_POLL_SECONDS);continue
                print(json.dumps(dict(stage='packing',asset=asset)),flush=True)
                manifest=pack_asset(args.database,asset,args.artifacts,checkpoint=checkpoint)
                if manifest:print(json.dumps(dict(changed=True,stage='ready',asset=asset)),flush=True)
            except InterruptedError:
                if args.once:return
                stop.wait(RETRY_DELAY_SECONDS)
            except Exception as error:
                if asset:
                    with connection(args.database) as db:
                        db.execute('INSERT INTO preview_pack_errors VALUES(?,?,1) ON CONFLICT(asset) DO UPDATE SET error=excluded.error,attempts=preview_pack_errors.attempts+1',(asset,str(error)))
                print(json.dumps(dict(error=str(error))),flush=True)
                if args.once:raise
                stop.wait(RETRY_DELAY_SECONDS)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path,required=True)
    parser.add_argument('--artifacts',type=Path,required=True)
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--projection',type=Path,help='Retire owned raw frames only after this projection references their verified atlas')
    main(parser.parse_args())
