"""Build/resume the derived search projection in bounded, audited transactions."""
import argparse
import fcntl
import json
from pathlib import Path
import time

from search_store import SearchStore, install_change_log


def main(args):
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.with_suffix('.writer.lock').open('a+b') as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        install_change_log(args.catalog)
        started = time.monotonic()
        with SearchStore(args.output) as store:
            batches = 0
            while True:
                pending = store.refresh(args.catalog, batch_size=args.batch_size)
                batches += 1
                checkpoint = dict(store.db.execute('SELECT * FROM checkpoint WHERE id=1').fetchone())
                print(json.dumps(dict(stage='catching_up' if pending else 'ready', batches=batches,
                                      seconds=time.monotonic()-started, checkpoint=checkpoint)), flush=True)
                if not pending:
                    break
            if args.index:
                store.seed_legacy(args.catalog, args.index, args.speech)
            result = dict(seconds=time.monotonic()-started, batches=batches,
                          files=store.db.execute('SELECT count(*) FROM files').fetchone()[0],
                          frames=store.db.execute('SELECT count(*) FROM frames').fetchone()[0],
                          speech_segments=store.db.execute('SELECT count(*) FROM speech').fetchone()[0])
            timings = []
            for _ in range(20):
                before = time.monotonic()
                store.page()
                timings.append((time.monotonic()-before)*1000)
            result['first_page_ms'] = timings[0]
            result['page_max_ms'] = max(timings)
            before = time.monotonic()
            hits = store.speech('dun')
            result['speech_ms'] = (time.monotonic()-before)*1000
            result['speech_hits'] = len(hits)
            result['compacted_events'] = store.compact_acknowledged_events(args.catalog)
            store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            args.output.with_suffix('.result.json').write_text(json.dumps(result, indent=2))
            print(json.dumps(result), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--batch-size', type=int, default=256)
    parser.add_argument('--index', type=Path)
    parser.add_argument('--speech', type=Path)
    main(parser.parse_args())
