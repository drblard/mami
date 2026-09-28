"""Measure indexed/keyset browsing of 50x media summaries without frame-array decoding."""
import argparse
import json
from pathlib import Path
import sqlite3
import time
from common import lock, progress, save, percentiles
from corpus import device_of


def main(args):
    with lock(args.run,'catalog-pages') as run:
        with sqlite3.connect(run/'source.sqlite') as source:
            base=[]
            for asset,payload in source.execute('SELECT asset,payload FROM media ORDER BY path'):
                value=json.loads(payload)
                meta=value.get('metadata') or {}
                captured=meta.get('sortDate','')
                captured=int(captured) if captured.isdigit() else 0
                summary={key:val for key,val in value.items() if key not in ('frames','match')}
                summary['thumbnail']=(value.get('frames') or [{}])[0].get('frame')
                base.append((asset,value['path'],captured,device_of(value),value['kind'],json.dumps(summary)))
        db=sqlite3.connect(run/f'catalog-{args.scale}x.sqlite')
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA cache_size=-32768')
        db.executescript('CREATE TABLE IF NOT EXISTS media(id INTEGER PRIMARY KEY,asset TEXT,path TEXT,captured INTEGER,camera TEXT,kind TEXT,payload TEXT);'
                         'CREATE TABLE IF NOT EXISTS checkpoint(replica INTEGER PRIMARY KEY);')
        cursor=db.execute('SELECT coalesce(max(replica)+1,0) FROM checkpoint').fetchone()[0]
        for replica in range(cursor,args.scale):
            with db:
                db.executemany('INSERT INTO media VALUES(?,?,?,?,?,?,?)',
                               [(replica*len(base)+i,*row) for i,row in enumerate(base)])
                db.execute('INSERT INTO checkpoint VALUES(?)',(replica,))
            progress(run,'catalog-pages',stage='build',done=replica+1,total=args.scale)
        with db:
            db.execute('CREATE INDEX IF NOT EXISTS media_date ON media(captured DESC,id DESC)')
            db.execute('CREATE INDEX IF NOT EXISTS media_camera_date ON media(camera,captured DESC,id DESC)')
            db.execute('CREATE INDEX IF NOT EXISTS media_kind_date ON media(kind,captured DESC,id DESC)')
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        timings=[];last=None;seen=set()
        for page in range(50):
            started=time.perf_counter()
            if last is None:
                rows=db.execute('SELECT captured,id,payload FROM media ORDER BY captured DESC,id DESC LIMIT 100').fetchall()
            else:
                rows=db.execute('SELECT captured,id,payload FROM media WHERE (captured,id)<(?,?) ORDER BY captured DESC,id DESC LIMIT 100',last).fetchall()
            decoded=[json.loads(row[2]) for row in rows]
            timings.append((time.perf_counter()-started)*1000)
            assert not (seen & {row[1] for row in rows}),'Keyset paging repeated a row'
            seen.update(row[1] for row in rows)
            last=rows[-1][:2]
        result=dict(rows=db.execute('SELECT count(*) FROM media').fetchone()[0],page_size=100,pages=len(timings),
                    first_page_ms=timings[0],latency=percentiles(timings),database_bytes=(run/f'catalog-{args.scale}x.sqlite').stat().st_size,
                    caveat='Prototype summary projection, not integrated UI. Synthetic replicas retain capture dates; originals never opened.')
        save(run/f'catalog-{args.scale}x-benchmark.json',result)
        progress(run,'catalog-pages',stage='complete',**result)
        db.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,required=True)
    parser.add_argument('--scale',type=int,default=50)
    main(parser.parse_args())
