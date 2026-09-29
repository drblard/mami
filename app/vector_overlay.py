"""Bounded exact delta over an immutable packed base, with atomic refreshes."""
from dataclasses import dataclass
import json

MAX_OVERLAY_ROWS = 32768
MAX_OVERLAY_ASSETS = 4096
MAX_EVENT_BATCH = 4096


@dataclass(frozen=True)
class OverlaySnapshot:
    cursor: int
    assets: dict
    samples: list
    matrix: object = None


class VectorOverlay:
    def __init__(self, base, projection):
        self.base = base
        self.projection = projection
        self.snapshot = OverlaySnapshot(base.manifest['projection_sequence'],{},[])

    def refresh(self, projection=None):
        import numpy as np
        db = (projection or self.projection).db
        previous = self.snapshot
        db.execute('BEGIN')
        try:
            state = db.execute('SELECT identity,epoch,sequence FROM checkpoint WHERE id=1').fetchone()
            if state['identity'] != self.base.manifest['source_identity'] or state['epoch'] != self.base.manifest['epoch']:
                raise ValueError('Vector generation belongs to a different catalog history')
            if state['sequence'] < self.base.manifest['sequence']:
                raise ValueError('Vector generation is newer than restored catalog')
            events = db.execute('SELECT sequence,asset FROM vector_events WHERE sequence>? ORDER BY sequence LIMIT ?',(previous.cursor,MAX_EVENT_BATCH)).fetchall()
            if not events:
                return False
            updated = dict(previous.assets)
            mapped = {}
            for asset in {row['asset'] for row in events}:
                if asset not in updated and len(updated)>=MAX_OVERLAY_ASSETS:
                    raise RuntimeError('Vector delta requires background compaction')
                metadata = db.execute('SELECT path,kind,camera,captured,shape,arrival FROM files WHERE asset=?',(asset,)).fetchone()
                entries=[]
                if metadata is not None:
                    replacement_count=db.execute('SELECT count(*) FROM embeddings WHERE asset=?',(asset,)).fetchone()[0]
                    projected_count=sum(len(value) for key,value in updated.items() if key!=asset)+replacement_count
                    if projected_count>MAX_OVERLAY_ROWS:
                        raise RuntimeError('Vector delta requires background compaction')
                    for row in db.execute('SELECT ordinal,vector_path,vector_row,frame,timestamp,crop FROM embeddings WHERE asset=? ORDER BY ordinal',(asset,)):
                        path=row['vector_path']
                        if row['vector_row'] is None:
                            vector=np.load(path,allow_pickle=False)
                        else:
                            if path not in mapped:mapped[path]=np.load(path,mmap_mode='r',allow_pickle=False)
                            vector=mapped[path][row['vector_row']]
                        if vector.shape!=(768,) or not np.isfinite(vector).all():
                            raise ValueError('Invalid incremental vector')
                        sample=dict(asset=asset,path=metadata['path'],kind=metadata['kind'],camera=metadata['camera'],
                                    captured=metadata['captured'],shape=metadata['shape'],arrival=metadata['arrival'],ordinal=row['ordinal'],frame=row['frame'],timestamp=row['timestamp'],crop=json.loads(row['crop'] or 'null'))
                        entries.append((sample,vector))
                updated[asset]=entries  # Empty entry masks deleted or no-longer-indexed assets.
            count=sum(len(value) for value in updated.values())
            if count>MAX_OVERLAY_ROWS:
                raise RuntimeError('Vector delta requires background compaction')
            samples=[sample for entries in updated.values() for sample,_ in entries]
            matrix=np.stack([vector for entries in updated.values() for _,vector in entries]) if count else None
            self.snapshot=OverlaySnapshot(events[-1]['sequence'],updated,samples,matrix)
            return True
        finally:
            db.rollback()  # End the read snapshot, never write to the projection.

    def search(self, query, *, paths=None, limit=60, scope=None):
        import numpy as np
        snapshot=self.snapshot
        scope=scope or {}
        assets=set(scope['assets']) if scope.get('assets') is not None else None
        hits=self.base.search(query,paths=paths,limit=limit,exclude_assets=snapshot.assets,
                              camera=scope.get('camera'),kind=scope.get('kind'),shape=scope.get('shape'),
                              from_date=scope.get('from'),through_date=scope.get('through'),
                              assets=assets,arrival_through=scope.get('arrivalThrough'))
        if snapshot.matrix is not None:
            scores=snapshot.matrix@query
            seen=set()
            for ordinal in np.argsort(-scores):
                sample=snapshot.samples[int(ordinal)]
                if sample['asset'] in seen or (paths is not None and sample['path'] not in paths):continue
                if assets is not None and sample['asset'] not in assets:continue
                if any(scope.get(key) is not None and sample[key]!=scope[key] for key in ('camera','kind','shape')):continue
                if scope.get('from') is not None and sample['captured']<scope['from']:continue
                if scope.get('through') is not None and (not sample['captured'] or sample['captured']>scope['through']):continue
                if scope.get('arrivalThrough') is not None and sample['arrival']>scope['arrivalThrough']:continue
                seen.add(sample['asset'])
                hits.append(dict(sample,score=float(scores[ordinal])))
                if len(seen)==limit:break
        return sorted(hits,key=lambda hit:(-hit['score'],hit['path']))[:limit]
