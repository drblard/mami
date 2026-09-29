"""Validate packing and raw-cache retirement with real cached frames, originals offline."""
import argparse
import contextlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import time


def main(args):
    args.output.mkdir(parents=True,exist_ok=False)
    sys.path.insert(0,str(args.bundle/'Contents/Resources'))
    from index_store import connection
    from model_config import PIPELINE
    from preview_atlas import pack_asset,retire_raw_frames
    from search_store import SearchStore,install_change_log
    database=args.output/'catalog.sqlite'
    with contextlib.closing(sqlite3.connect(args.catalog.resolve().as_uri()+'?mode=ro',uri=True,timeout=10)) as source:
        row=source.execute("SELECT j.asset,j.path,count(*) FROM index_jobs j JOIN index_units u ON u.asset=j.asset WHERE j.kind='video' AND j.state='complete' AND u.stage='frame' AND u.pipeline=? GROUP BY j.asset HAVING count(*) BETWEEN 30 AND 120 ORDER BY j.capture_time DESC LIMIT 1",(PIPELINE,)).fetchone()
        if row is None:raise ValueError('No completed real-video fixture found')
        asset,original_path,_=row
        with contextlib.closing(sqlite3.connect(database)) as target:source.backup(target)
    artifacts=args.output/'artifacts';folder=artifacts/asset.removeprefix('sha256:');folder.mkdir(parents=True)
    raw_bytes=0
    with connection(database) as db:
        db.execute('DELETE FROM media WHERE asset!=?',(asset,))
        db.execute('DELETE FROM index_units WHERE asset!=?',(asset,))
        db.execute('DELETE FROM index_jobs WHERE asset!=?',(asset,))
        media=json.loads(db.execute('SELECT payload FROM media WHERE asset=?',(asset,)).fetchone()[0])
        frames=[]
        for ordinal,payload in db.execute("SELECT ordinal,payload FROM index_units WHERE asset=? AND pipeline=? AND stage='frame' ORDER BY ordinal",(asset,PIPELINE)).fetchall():
            sample=json.loads(payload)
            target=folder/f'{ordinal}.jpg';shutil.copy2(sample['frame'],target);raw_bytes+=target.stat().st_size
            sample['frame']=str(target);frames.append(sample)
            db.execute("UPDATE index_units SET payload=? WHERE asset=? AND pipeline=? AND stage='frame' AND ordinal=?",(json.dumps(sample),asset,PIPELINE,ordinal))
        media.update(frames=frames,match=frames[0],url='file:///unmounted/fixture-original.mp4')
        db.execute('UPDATE media SET path=?,payload=? WHERE asset=?',('/unmounted/fixture-original.mp4',json.dumps(media),asset))
        db.execute('UPDATE index_jobs SET path=? WHERE asset=?',('/unmounted/fixture-original.mp4',asset))
        vectors_before={ordinal:json.loads(payload)['vector'] for ordinal,payload in db.execute("SELECT ordinal,payload FROM index_units WHERE stage='embedding' AND asset=?",(asset,))}
    install_change_log(database)
    started=time.monotonic()
    manifest=pack_asset(database,asset,artifacts)
    if manifest is None:raise ValueError('Fixture was not packed')
    elapsed=time.monotonic()-started
    projection=args.output/'search.sqlite'
    with SearchStore(projection) as store:
        while store.refresh(database):pass
        removed=0
        while True:
            count=retire_raw_frames(database,asset,artifacts,projection)
            if count is None:raise ValueError('Packed fixture did not qualify for retirement')
            removed+=count
            if count==0:break
        projected=store.page()['items'][0]
        if not projected['match'].get('crop'):raise ValueError('Packed crop missing from offline summary')
    with connection(database) as db:
        vectors_after={ordinal:json.loads(payload)['vector'] for ordinal,payload in db.execute("SELECT ordinal,payload FROM index_units WHERE stage='embedding' AND asset=?",(asset,))}
    assert vectors_before==vectors_after
    assert removed==len(frames)
    assert not list(folder.glob('*.jpg'))
    sheets={Path(frame['sample']['frame']) for frame in manifest['frames']}
    result=dict(status='passed',original_fixture_name=Path(original_path).name,frames=len(frames),sheets=len(sheets),
                raw_bytes=raw_bytes,packed_bytes=sum(path.stat().st_size for path in sheets),pack_seconds=elapsed,
                retired_copied_raw_frames=removed,embedding_references_unchanged=True,original_url='/unmounted/fixture-original.mp4',
                scope='Isolated copies of real cached frames; originals never opened; live cache untouched.')
    (args.output/'result.json').write_text(json.dumps(result,indent=2));print(json.dumps(result,indent=2),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle',type=Path,required=True)
    parser.add_argument('--catalog',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    main(parser.parse_args())
