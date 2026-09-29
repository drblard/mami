"""Opt-in persistent search path: native text encoder, packed vectors, SQLite FTS."""
import contextlib
from functools import lru_cache
import json
from pathlib import Path
import sys
import time
import threading

from packed_vectors import PackedIndex
from search_logic import combine_hits
from search_store import SearchStore
from text_encoder import NativeEncoder
from vector_overlay import VectorOverlay
from vector_sync import resolve_generation


def run(args):
    if not args.native_encoder or not args.projection:
        raise ValueError('Packed search requires a native encoder and ready SQLite projection')
    executable=Path(args.native_executable) if args.native_executable else Path(__file__).resolve().parent.parent/'MacOS/Mami'
    index=encoder=projection=None
    stop=threading.Event()
    generation_lock=threading.Lock()
    generation_state={}
    refresh_thread=None
    try:
        with contextlib.redirect_stdout(sys.stderr):
            generation=resolve_generation(args.packed_index)
            index=PackedIndex(generation)
            projection=SearchStore(args.projection,read_only=True)
            checkpoint=projection.db.execute('SELECT identity,epoch FROM checkpoint WHERE id=1 AND ready=1').fetchone()
            if checkpoint is None or checkpoint['identity']!=index.manifest['source_identity'] or checkpoint['epoch']!=index.manifest['epoch']:
                raise ValueError('Packed index and catalog projection identities differ')
            encoder=NativeEncoder(executable,args.native_encoder)
            encode=lru_cache(maxsize=128)(encoder.encode)
            overlay=VectorOverlay(index,projection)
            overlay.refresh()
            warm_vector=encode('a photo')
            index.search(warm_vector,limit=1)
            generation_state.update(index=index,overlay=overlay,path=generation)
        def refresh():
            with SearchStore(args.projection,read_only=True) as reader:
                while not stop.wait(1):
                    replacement=None
                    try:
                        latest=resolve_generation(args.packed_index)
                        if latest!=generation_state['path']:
                            replacement=PackedIndex(latest)
                            next_overlay=VectorOverlay(replacement,reader)
                            next_overlay.refresh(reader)
                            replacement.search(warm_vector,limit=1)
                            if stop.is_set():
                                replacement.close()
                                return
                            with generation_lock:
                                previous=generation_state['index']
                                generation_state.update(index=replacement,overlay=next_overlay,path=latest)
                                replacement=None
                                previous.close()
                        else:
                            generation_state['overlay'].refresh(reader)
                    except Exception as error:
                        if replacement is not None:replacement.close()
                        print(f'Vector delta refresh: {error}',file=sys.stderr,flush=True)
        refresh_thread=threading.Thread(target=refresh,daemon=True)
        refresh_thread.start()
        print(json.dumps(dict(ready=True,samples=index.manifest['rows'])),flush=True)
        for line in sys.stdin:
            started=time.monotonic()
            try:
                request=json.loads(line)
                query=request['query'].strip()
                if not query or len(query)>2000:
                    raise ValueError('Query must contain between 1 and 2000 characters')
                mode=request.get('mode','both')
                if mode not in ('visual','speech','both'):
                    raise ValueError('Unknown search mode')
                allowed=set(request['paths']) if 'paths' in request else None
                scope=request.get('scope') or {}
                visual=[]
                if mode!='speech':
                    feature=encode(query)
                    with generation_lock:
                        visual=generation_state['overlay'].search(feature,paths=allowed,scope=scope)
                    for hit in visual:
                        frame=projection.db.execute('SELECT frame,crop FROM frames WHERE asset=? AND ordinal=?',(hit['asset'],hit['ordinal'])).fetchone()
                        if frame:
                            hit['frame']=frame['frame']
                            hit['crop']=json.loads(frame['crop'] or 'null')
                spoken=[]
                if mode!='visual':
                    # Apply path scopes before the per-file result limit. This
                    # opt-in bridge uses the existing protocol while native
                    # structured camera/date filters are integrated separately.
                    for hit in projection.speech(query,allowed_paths=allowed,scope=scope):
                        spoken.append(dict(path=hit['path'],kind=hit['kind'],timestamp=hit['timestamp'],
                                           frame=hit['frame'] or '',crop=hit.get('crop'),evidence=hit['evidence'],score=1.0))
                hits=visual if mode=='visual' else spoken if mode=='speech' else combine_hits(visual,spoken)
                reply=dict(hits=hits,elapsed=time.monotonic()-started,indexed_samples=generation_state['index'].manifest['rows'])
            except Exception as error:
                reply=dict(error=str(error))
            print(json.dumps(reply,ensure_ascii=False),flush=True)
    finally:
        stop.set()
        if refresh_thread is not None:refresh_thread.join(timeout=2)
        with generation_lock:
            current=generation_state.get('index',index)
            if current is not None:current.close()
        if projection is not None:projection.close()
        if encoder is not None:encoder.close()
