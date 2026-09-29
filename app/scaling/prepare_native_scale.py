"""Create native-readable 50x fixtures from retained corpus/quantization experiments.

Only an explicitly named NEW benchmark directory is written. Synthetic asset IDs
are namespaced; source originals are referenced for display but never modified.
"""
import argparse
import contextlib
import json
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time


def main(args):
    import numpy as np
    sys.path.insert(0,str(args.resources))
    from search_store import SearchStore
    from packed_vectors import FORMAT_VERSION
    from model_config import VISUAL_MODEL
    from vector_sync import publish
    args.output.mkdir(parents=True,exist_ok=False)
    catalog_dir=args.output/'catalog';catalog_dir.mkdir()
    shutil.copy2(args.source_catalog,catalog_dir/'catalog.sqlite')
    with contextlib.closing(sqlite3.connect(args.source_projection)) as source:
        source.row_factory=sqlite3.Row
        checkpoint=dict(source.execute('SELECT * FROM checkpoint').fetchone())
        metadata={row['path']:dict(row) for row in source.execute('SELECT * FROM files')}
    with contextlib.closing(sqlite3.connect(args.corpus/'corpus.sqlite')) as source:
        frames=source.execute('SELECT path,timestamp,frame FROM frames ORDER BY id').fetchall()
        segments=source.execute('SELECT path,start,text FROM segments ORDER BY id').fetchall()
    paths=sorted({row[0] for row in frames});ids={path:i for i,path in enumerate(paths)}
    base_ids=np.asarray([ids[row[0]] for row in frames],dtype=np.int32)
    total=len(frames)*args.scale
    with SearchStore(args.output/'search.sqlite') as projection:
        with projection.db:
            projection.db.execute('INSERT INTO checkpoint VALUES(1,?,?,NULL,1,?,?)',
                                  (checkpoint['identity'],checkpoint['sequence'],checkpoint['epoch'],checkpoint['anchor']))
            projection.db.execute('INSERT INTO vector_events VALUES(?,?)',(1,'scale-fixture'))
        vectors=args.output/'vectors';vectors.mkdir()
        owner='native-scale-fixture'
        (vectors/'owner.json').write_text(json.dumps(dict(id=owner)))
        generation=vectors/'generation-fixture';generation.mkdir()
        row_file_ids=np.lib.format.open_memmap(generation/'file_ids.npy',mode='w+',dtype=np.int32,shape=(total,))
        with contextlib.closing(sqlite3.connect(generation/'rows.sqlite')) as rows:
            rows.executescript('CREATE TABLE files(id INTEGER PRIMARY KEY,asset TEXT UNIQUE,path TEXT,kind TEXT,camera TEXT,captured TEXT,shape TEXT,arrival INTEGER);'
                              'CREATE INDEX file_camera ON files(camera); CREATE INDEX file_date ON files(captured);'
                              'CREATE TABLE vectors(id INTEGER PRIMARY KEY,file_id INTEGER,ordinal INTEGER,frame TEXT,timestamp REAL,crop TEXT);')
            for replica in range(args.scale):
                with projection.db,rows:
                    suffix=f'#scale-{replica}'
                    for path in paths:
                        value=metadata[path];asset=value['asset']+suffix;logical=path+suffix
                        media=json.loads(value['summary']);media['path']=logical;media['assetID']=asset;media['match']['path']=logical
                        media['frames']=[]
                        projection.db.execute('INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?)',
                                              (asset,logical,value['source_url'],value['kind'],value['camera'],value['captured'],json.dumps(media),value['shape'],1))
                        rows.execute('INSERT INTO files VALUES(?,?,?,?,?,?,?,?)',
                                     (replica*len(paths)+ids[path],asset,logical,value['kind'],value['camera'],value['captured'],value['shape'],1))
                    ordinals={}
                    for offset,(path,timestamp,frame) in enumerate(frames):
                        ordinal=ordinals.get(path,0);ordinals[path]=ordinal+1
                        asset=metadata[path]['asset']+suffix
                        projection.db.execute('INSERT INTO frames VALUES(?,?,?,?,?)',(asset,ordinal,timestamp,frame,'null'))
                        rows.execute('INSERT INTO vectors VALUES(?,?,?,?,?,?)',
                                     (replica*len(frames)+offset,replica*len(paths)+ids[path],ordinal,frame,timestamp,'null'))
                    for ordinal,(path,timestamp,text) in enumerate(segments):
                        value=metadata.get(path)
                        if value is None:continue
                        camera_key=hashlib.sha256(value['camera'].encode()).hexdigest()
                        captured=value['captured']
                        scope=f'c{camera_key} d{captured[:8]} m{captured[:6]} y{captured[:4]}'
                        projection.db.execute('INSERT INTO speech(asset,chunk,ordinal,start,text,scope) VALUES(?,?,?,?,?,?)',
                                              (value['asset']+suffix,0,ordinal,timestamp,text,scope))
                row_file_ids[replica*len(frames):(replica+1)*len(frames)]=base_ids+replica*len(paths)
                print(json.dumps(dict(stage='metadata',replica=replica+1,total=args.scale)),flush=True)
            if rows.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Invalid vector row map')
        row_file_ids.flush()
        full=np.memmap(args.quantized/'full.f32',mode='r',dtype=np.float32,shape=(total,768))
        destination=np.lib.format.open_memmap(generation/'vectors.npy',mode='w+',dtype=np.float32,shape=full.shape)
        for start in range(0,total,65536):destination[start:start+65536]=full[start:start+65536]
        destination.flush();del destination,full
        shutil.copy2(args.quantized/f'mlx-{args.scale}x-4bit.safetensors',generation/'quantized.safetensors')
        (generation/'.readers').touch()
        manifest=dict(version=FORMAT_VERSION,model=VISUAL_MODEL,dimensions=768,rows=total,bits=4,group_size=64,
                      source_identity=checkpoint['identity'],sequence=checkpoint['sequence'],epoch=checkpoint['epoch'],
                      anchor=checkpoint['anchor'],projection_sequence=1,owner=owner,created=time.time())
        (generation/'manifest.json').write_text(json.dumps(manifest,indent=2))
        publish(vectors,generation)
        projection.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        speech_count=projection.db.execute('SELECT count(*) FROM speech').fetchone()[0]
    report=dict(rows=total,files=len(paths)*args.scale,speech_segments=speech_count,output=str(args.output),
                caveat='Synthetic 50x: repeated source metadata/images, unique perturbed vectors. Measures native load/search capacity, not unseen-footage relevance or cold-reboot startup.')
    (args.output/'fixture.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--resources',type=Path,required=True)
    parser.add_argument('--source-catalog',type=Path,required=True)
    parser.add_argument('--source-projection',type=Path,required=True)
    parser.add_argument('--corpus',type=Path,required=True)
    parser.add_argument('--quantized',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--scale',type=int,default=50)
    main(parser.parse_args())
