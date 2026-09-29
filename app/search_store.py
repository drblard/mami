"""Rebuildable SQLite search projection fed by a durable catalog change log.

The source transaction commits its event with its data. Projection updates and
the consumer cursor commit together, so replay after interruption is idempotent.
No source media or personal data is modified. Vector-engine integration consumes
the normalized embeddings table rather than opening the entire artifact tree.
"""
import contextlib
from datetime import datetime,date,timedelta
import calendar
import json
from pathlib import Path
import sqlite3
import uuid
from urllib.parse import unquote, urlparse

from index_store import connection as catalog_transaction
from model_config import PIPELINE
from search_logic import words

SCHEMA_VERSION = 6
READ_BATCH_SIZE = 256
QUERY_CACHE_KIB = 64 * 1024


def camera_name(media):
    metadata = media.get('metadata') or {}
    parts = Path(unquote(urlparse(media.get('url', '')).path)).parts
    if metadata.get('source') != 'iCloud' and 'Originals' in parts:
        index = len(parts)-1-list(reversed(parts)).index('Originals')
        if index+1 < len(parts) and parts[index+1] not in ('iCloud', 'iCloud-Photos'):
            return parts[index+1]
    if metadata.get('camera'):
        return metadata['camera']
    if metadata.get('camera') is None and len(metadata.get('details', [])) > 1:
        return metadata['details'][-1]
    return 'iCloud · Device unavailable' if metadata.get('source') == 'iCloud' or 'iCloud' in parts else 'Unknown device'


def fts_expression(query):
    terms = words(query)
    return ' AND '.join('"'+term+'"'+('*' if index == len(terms)-1 else '')
                        for index, term in enumerate(terms))


def date_scope_tokens(start,end):
    """Cover an inclusive date range with disjoint year/month/day postings."""
    first=datetime.strptime(start[:8],'%Y%m%d').date()
    last=datetime.strptime(end[:8],'%Y%m%d').date()
    result=[]
    current=first
    while current<=last:
        year_end=date(current.year,12,31)
        month_end=date(current.year,current.month,calendar.monthrange(current.year,current.month)[1])
        if current.month==1 and current.day==1 and year_end<=last:
            result.append(f'y{current.year:04d}')
            if year_end==last:break
            current=year_end+timedelta(days=1)
        elif current.day==1 and month_end<=last:
            result.append(f'm{current.year:04d}{current.month:02d}')
            if month_end==last:break
            current=month_end+timedelta(days=1)
        else:
            result.append(f'd{current.year:04d}{current.month:02d}{current.day:02d}')
            if current==last:break
            current+=timedelta(days=1)
    return result


def install_change_log(source):
    """One-time setup. Incremental producers need no application-level dual writes."""
    with catalog_transaction(source) as db:
        db.execute('CREATE TABLE IF NOT EXISTS search_log_info(id INTEGER PRIMARY KEY CHECK(id=1),epoch TEXT NOT NULL)')
        db.execute('INSERT OR IGNORE INTO search_log_info VALUES(1,?)', (uuid.uuid4().hex,))
        db.execute('CREATE TABLE IF NOT EXISTS search_events(sequence INTEGER PRIMARY KEY AUTOINCREMENT,asset TEXT NOT NULL,token TEXT NOT NULL DEFAULT (lower(hex(randomblob(16)))))')
        for table in ('media', 'index_units'):
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                continue
            had_trigger = db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?", (f'search_event_{table}_INSERT',)).fetchone() is not None
            for operation in ('INSERT', 'UPDATE', 'DELETE'):
                references = ['OLD', 'NEW'] if operation == 'UPDATE' else ['OLD' if operation == 'DELETE' else 'NEW']
                body = ' '.join(f'INSERT INTO search_events(asset) VALUES({reference}.asset);' for reference in references)
                db.execute(f'CREATE TRIGGER IF NOT EXISTS search_event_{table}_{operation} AFTER {operation} ON {table} BEGIN {body} END')
            if table == 'index_units' and not had_trigger:
                # Index tables may appear after the projection worker starts.
                # Catch units committed before their change triggers were installed.
                db.execute('INSERT INTO search_events(asset) SELECT DISTINCT asset FROM index_units')


class SearchStore:
    def __init__(self, path, read_only=False):
        self.path = Path(path)
        if not read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True) if read_only else sqlite3.connect(self.path)
        try:
            self._initialize(read_only)
        except BaseException:
            self.db.close()
            raise

    def _initialize(self, read_only):
        self.db.row_factory = sqlite3.Row
        self.db.execute(f'PRAGMA cache_size=-{QUERY_CACHE_KIB}')
        if not read_only:
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA foreign_keys=ON')
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        if version == 5 and not read_only:
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                self._create_facets()
                self.db.execute('INSERT INTO camera_counts SELECT camera,count(*) FROM files GROUP BY camera')
                self.db.execute('INSERT INTO browse_counts SELECT camera,shape,kind,substr(captured,1,8),count(*) FROM files GROUP BY camera,shape,kind,substr(captured,1,8)')
                self.db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
            version = SCHEMA_VERSION
        if read_only and version != SCHEMA_VERSION:
            self.close()
            raise ValueError('Search projection is not initialized')
        if version not in (0, SCHEMA_VERSION):
            self.close()
            raise ValueError('Unsupported search projection schema')
        if version == 0:
            self.db.executescript('''
                BEGIN IMMEDIATE;
                CREATE TABLE checkpoint(id INTEGER PRIMARY KEY CHECK(id=1),identity TEXT NOT NULL,sequence INTEGER NOT NULL,seed_after TEXT,ready INTEGER NOT NULL,epoch TEXT NOT NULL,anchor TEXT);
                CREATE TABLE files(asset TEXT PRIMARY KEY,path TEXT NOT NULL,source_url TEXT NOT NULL,kind TEXT NOT NULL,camera TEXT NOT NULL,captured TEXT NOT NULL,summary TEXT NOT NULL,shape TEXT NOT NULL,arrival INTEGER NOT NULL);
                CREATE INDEX files_date ON files(captured DESC,asset);
                CREATE INDEX files_oldest ON files((captured=''),captured,asset);
                CREATE INDEX files_camera_date ON files(camera,captured DESC,asset);
                CREATE INDEX files_path ON files(path);
                CREATE INDEX files_arrival ON files(arrival);
                CREATE INDEX files_shape_date ON files(shape,captured DESC,asset);
                CREATE TABLE frames(asset TEXT NOT NULL REFERENCES files(asset) ON DELETE CASCADE,ordinal INTEGER NOT NULL,timestamp REAL,frame TEXT NOT NULL,crop TEXT,PRIMARY KEY(asset,ordinal));
                CREATE INDEX frames_time ON frames(asset,timestamp);
                CREATE TABLE embeddings(asset TEXT NOT NULL REFERENCES files(asset) ON DELETE CASCADE,ordinal INTEGER NOT NULL,vector_path TEXT NOT NULL,vector_row INTEGER,frame TEXT NOT NULL,timestamp REAL,crop TEXT,PRIMARY KEY(asset,ordinal));
                CREATE TABLE legacy_units(asset TEXT NOT NULL,stage TEXT NOT NULL,ordinal INTEGER NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(asset,stage,ordinal));
                CREATE TABLE seeds(name TEXT PRIMARY KEY,digest TEXT NOT NULL);
                CREATE TABLE vector_events(sequence INTEGER PRIMARY KEY AUTOINCREMENT,asset TEXT NOT NULL);
                CREATE TABLE speech(id INTEGER PRIMARY KEY,asset TEXT NOT NULL REFERENCES files(asset) ON DELETE CASCADE,chunk INTEGER NOT NULL,ordinal INTEGER NOT NULL,start REAL NOT NULL,text TEXT NOT NULL,scope TEXT NOT NULL,UNIQUE(asset,chunk,ordinal));
                CREATE INDEX speech_asset ON speech(asset);
                CREATE VIRTUAL TABLE speech_fts USING fts5(text,scope,content=speech,content_rowid=id,tokenize='unicode61 remove_diacritics 2',prefix='1 2 3 4');
                CREATE TRIGGER speech_insert AFTER INSERT ON speech BEGIN INSERT INTO speech_fts(rowid,text,scope) VALUES(NEW.id,NEW.text,NEW.scope); END;
                CREATE TRIGGER speech_delete AFTER DELETE ON speech BEGIN INSERT INTO speech_fts(speech_fts,rowid,text,scope) VALUES('delete',OLD.id,OLD.text,OLD.scope); END;
            ''')
            with self.db:
                self._create_facets()
                self.db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')

    def _create_facets(self):
        self.db.execute('CREATE TABLE camera_counts(camera TEXT PRIMARY KEY,count INTEGER NOT NULL)')
        self.db.execute('CREATE TABLE browse_counts(camera TEXT NOT NULL,shape TEXT NOT NULL,kind TEXT NOT NULL,day TEXT NOT NULL,count INTEGER NOT NULL,PRIMARY KEY(camera,shape,kind,day))')
        self.db.execute('''CREATE TRIGGER file_camera_insert AFTER INSERT ON files BEGIN
            INSERT INTO camera_counts VALUES(NEW.camera,1) ON CONFLICT(camera) DO UPDATE SET count=count+1;
            INSERT INTO browse_counts VALUES(NEW.camera,NEW.shape,NEW.kind,substr(NEW.captured,1,8),1)
                ON CONFLICT(camera,shape,kind,day) DO UPDATE SET count=count+1;
        END''')
        self.db.execute('''CREATE TRIGGER file_camera_delete AFTER DELETE ON files BEGIN
            UPDATE camera_counts SET count=count-1 WHERE camera=OLD.camera;
            DELETE FROM camera_counts WHERE camera=OLD.camera AND count=0;
            UPDATE browse_counts SET count=count-1 WHERE camera=OLD.camera AND shape=OLD.shape AND kind=OLD.kind AND day=substr(OLD.captured,1,8);
            DELETE FROM browse_counts WHERE camera=OLD.camera AND shape=OLD.shape AND kind=OLD.kind AND day=substr(OLD.captured,1,8) AND count=0;
        END''')
        self.db.execute('''CREATE TRIGGER file_camera_update AFTER UPDATE OF camera,shape,kind,captured ON files
            WHEN NEW.camera!=OLD.camera OR NEW.shape!=OLD.shape OR NEW.kind!=OLD.kind OR NEW.captured!=OLD.captured BEGIN
            UPDATE camera_counts SET count=count-1 WHERE camera=OLD.camera;
            DELETE FROM camera_counts WHERE camera=OLD.camera AND count=0;
            INSERT INTO camera_counts VALUES(NEW.camera,1) ON CONFLICT(camera) DO UPDATE SET count=count+1;
            UPDATE browse_counts SET count=count-1 WHERE camera=OLD.camera AND shape=OLD.shape AND kind=OLD.kind AND day=substr(OLD.captured,1,8);
            DELETE FROM browse_counts WHERE camera=OLD.camera AND shape=OLD.shape AND kind=OLD.kind AND day=substr(OLD.captured,1,8) AND count=0;
            INSERT INTO browse_counts VALUES(NEW.camera,NEW.shape,NEW.kind,substr(NEW.captured,1,8),1)
                ON CONFLICT(camera,shape,kind,day) DO UPDATE SET count=count+1;
        END''')

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _replace_asset(self, source, asset):
        previous = self.db.execute('SELECT arrival,shape,path,kind,camera,captured FROM files WHERE asset=?',(asset,)).fetchone()
        previous_vectors = [tuple(row) for row in self.db.execute('SELECT ordinal,vector_path,vector_row,timestamp FROM embeddings WHERE asset=? ORDER BY ordinal',(asset,))]
        previous_frame = self.db.execute('SELECT frame,crop FROM frames WHERE asset=? AND ordinal=0',(asset,)).fetchone()
        row = source.execute('SELECT payload FROM media WHERE asset=? ORDER BY path LIMIT 1', (asset,)).fetchone()
        self.db.execute('DELETE FROM files WHERE asset=?', (asset,))
        if row is None:
            if previous is not None:self.db.execute('INSERT INTO vector_events(asset) VALUES(?)',(asset,))
            return
        if previous is None:self.db.execute('INSERT INTO vector_events(asset) VALUES(?)',(asset,))
        media = json.loads(row[0])
        metadata = media.get('metadata') or {}
        camera = camera_name(media)
        captured = metadata.get('sortDate') or ''
        frames = media.get('frames', [])
        shape=previous['shape'] if previous else 'unknown'
        first_frame=frames[0]['frame'] if frames else None
        if previous and previous_frame and previous_frame[0]==first_frame and previous_frame[1]==json.dumps(frames[0].get('crop')):
            shape=previous['shape']
        elif frames and frames[0].get('sourceSize'):
            width,height=frames[0]['sourceSize']
            shape='square' if abs(width-height)<=1 else 'vertical' if height>width else 'horizontal'
        elif first_frame:
            try:
                from PIL import Image
                with Image.open(first_frame) as image:
                    width,height=image.size
                    if frames[0].get('crop'):
                        _,_,width,height=frames[0]['crop']
                    if frames[0].get('sourceSize'):
                        width,height=frames[0]['sourceSize']
                    shape='square' if abs(width-height)<=1 else 'vertical' if height>width else 'horizontal'
            except (ImportError,OSError):
                pass  # Metadata projection still works with offline/missing preview files.
        arrival = previous['arrival'] if previous else self.db.execute('SELECT max(sequence) FROM vector_events').fetchone()[0]
        summary = {key: value for key, value in media.items() if key != 'frames'}
        summary['frames'] = []  # Native Media decoding; full frames are fetched on demand.
        summary['frameCount'] = len(frames)
        summary['cachedFormat'] = shape
        self.db.execute('INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?)',
                        (asset, media['path'], media['url'], media['kind'], camera, captured, json.dumps(summary),shape,arrival))
        self.db.executemany('INSERT INTO frames VALUES(?,?,?,?,?)',
                            [(asset, ordinal, frame.get('timestamp'), frame['frame'],json.dumps(frame.get('crop'))) for ordinal, frame in enumerate(frames)])
        import hashlib
        camera_key = hashlib.sha256(camera.encode()).hexdigest()
        scope = f'c{camera_key} d{captured[:8]} m{captured[:6]} y{captured[:4]}'
        units = {(row['stage'], row['ordinal']): row['payload'] for row in
                 self.db.execute('SELECT stage,ordinal,payload FROM legacy_units WHERE asset=?', (asset,))}
        if source.execute("SELECT 1 FROM sqlite_master WHERE name='index_units'").fetchone():
            units.update({(stage, ordinal): payload for stage, ordinal, payload in source.execute(
                "SELECT stage,ordinal,payload FROM index_units WHERE asset=? AND pipeline=? AND stage IN ('embedding','speech')", (asset, PIPELINE))})
        for (stage, ordinal), payload in units.items():
            value = json.loads(payload)
            if stage == 'embedding':
                sample = value['sample']
                self.db.execute('INSERT INTO embeddings VALUES(?,?,?,?,?,?,?)',
                                (asset, ordinal, value['vector'], value.get('vector_row'), sample['frame'], sample.get('timestamp'),json.dumps(sample.get('crop'))))
            else:
                self.db.executemany('INSERT INTO speech(asset,chunk,ordinal,start,text,scope) VALUES(?,?,?,?,?,?)',
                                    [(asset, ordinal, index, segment['start'], segment['text'], scope)
                                     for index, segment in enumerate(value['segments'])])
        if previous is not None:
            current_vectors=[tuple(row) for row in self.db.execute('SELECT ordinal,vector_path,vector_row,timestamp FROM embeddings WHERE asset=? ORDER BY ordinal',(asset,))]
            old_scope=tuple(previous[key] for key in ('path','kind','camera','captured','shape'))
            new_scope=(media['path'],media['kind'],camera,captured,shape)
            if previous_vectors!=current_vectors or old_scope!=new_scope:
                self.db.execute('INSERT INTO vector_events(asset) VALUES(?)',(asset,))

    def seed_legacy(self, catalog, index, speech=None):
        """Seed immutable experimental outputs once; use matrix-row references.

        Content identity comes from the catalog, never filenames alone. New
        incremental units supersede legacy entries for the same asset/ordinal.
        """
        import hashlib
        index = Path(index).resolve()
        samples_path = index/'samples.json'
        documents = [(samples_path, samples_path.read_bytes())]
        if speech:
            documents += [(path, path.read_bytes()) for path in sorted(Path(speech).glob('*.json')) if path.stem.isdigit()]
        digest = hashlib.sha256()
        for path, contents in documents:
            digest.update(str(path).encode());digest.update(contents)
        key = str(index)
        existing = self.db.execute('SELECT digest FROM seeds WHERE name=?', (key,)).fetchone()
        if existing:
            if existing[0] != digest.hexdigest():
                raise ValueError('Legacy seed changed; rebuild projection')
            return False
        with contextlib.closing(sqlite3.connect(Path(catalog).resolve().as_uri()+'?mode=ro',uri=True,timeout=10)) as source:
            source.execute('BEGIN')
            by_path = {json.loads(payload)['path']: asset for asset,payload in source.execute('SELECT asset,payload FROM media')}
            samples = json.loads(documents[0][1])['samples']
            ordinals, affected = {}, set()
            with self.db:
                for row, sample in enumerate(samples):
                    asset = by_path.get(sample['path'])
                    if asset is None:
                        continue
                    ordinal = ordinals.get(asset, 0);ordinals[asset] = ordinal+1
                    value = dict(vector=str(index/'embeddings.npy'),vector_row=row,sample=sample)
                    self.db.execute('INSERT OR REPLACE INTO legacy_units VALUES(?,?,?,?)', (asset,'embedding',ordinal,json.dumps(value)))
                    affected.add(asset)
                for path, contents in documents[1:]:
                    value = json.loads(contents)
                    asset = by_path.get(value['path'])
                    if asset is None:
                        continue
                    payload = dict(segments=value.get('transcript',{}).get('segments',[]))
                    # A reserved negative ordinal cannot collide with incremental chunks.
                    self.db.execute('INSERT OR REPLACE INTO legacy_units VALUES(?,?,?,?)', (asset,'speech',-int(path.stem)-1,json.dumps(payload)))
                    affected.add(asset)
                for asset in affected:
                    self._replace_asset(source, asset)
                self.db.execute('INSERT INTO seeds VALUES(?,?)', (key,digest.hexdigest()))
        return True

    def refresh(self, catalog, batch_size=READ_BATCH_SIZE):
        """One bounded batch; returns True while more seed/events are pending."""
        if batch_size <= 0:
            raise ValueError('Batch size must be positive')
        with contextlib.closing(sqlite3.connect(Path(catalog).resolve().as_uri()+'?mode=ro', uri=True, timeout=10)) as source:
            source.execute('BEGIN')
            identity = source.execute('SELECT identity FROM state WHERE id=1').fetchone()[0]
            epoch = source.execute('SELECT epoch FROM search_log_info WHERE id=1').fetchone()[0]
            checkpoint = self.db.execute('SELECT * FROM checkpoint WHERE id=1').fetchone()
            if checkpoint and (checkpoint['identity'] != identity or checkpoint['epoch'] != epoch):
                raise ValueError('Projection belongs to another catalog; rebuild in a new directory')
            if checkpoint and checkpoint['sequence']:
                anchor = source.execute('SELECT token FROM search_events WHERE sequence=?', (checkpoint['sequence'],)).fetchone()
                if anchor is None or anchor[0] != checkpoint['anchor']:
                    raise ValueError('Catalog history was restored or truncated; rebuild the search projection')
            with self.db:
                if checkpoint is None:
                    sequence = source.execute('SELECT coalesce(max(sequence),0) FROM search_events').fetchone()[0]
                    anchor = source.execute('SELECT token FROM search_events WHERE sequence=?', (sequence,)).fetchone()
                    self.db.execute('INSERT INTO checkpoint VALUES(1,?,?,NULL,0,?,?)', (identity, sequence, epoch, anchor[0] if anchor else None))
                    checkpoint = self.db.execute('SELECT * FROM checkpoint WHERE id=1').fetchone()
                if not checkpoint['ready']:
                    assets = [row[0] for row in source.execute('SELECT DISTINCT asset FROM media WHERE ? IS NULL OR asset>? ORDER BY asset LIMIT ?',
                                                              (checkpoint['seed_after'], checkpoint['seed_after'], batch_size))]
                    for asset in assets:
                        self._replace_asset(source, asset)
                    if assets:
                        self.db.execute('UPDATE checkpoint SET seed_after=? WHERE id=1', (assets[-1],))
                    if len(assets) < batch_size:
                        self.db.execute('UPDATE checkpoint SET ready=1 WHERE id=1')
                    return True
                events = source.execute('SELECT sequence,asset,token FROM search_events WHERE sequence>? ORDER BY sequence LIMIT ?',
                                        (checkpoint['sequence'], batch_size)).fetchall()
                for asset in sorted({row[1] for row in events}):
                    self._replace_asset(source, asset)
                if events:
                    self.db.execute('UPDATE checkpoint SET sequence=?,anchor=? WHERE id=1', (events[-1][0], events[-1][2]))
                return len(events) == batch_size

    def page(self, limit=100, after=None, camera=None):
        if not 1 <= limit <= 1000:
            raise ValueError('Page size must be between 1 and 1000')
        clauses, params = [], []
        if camera is not None:
            clauses.append('camera=?'); params.append(camera)
        if after:
            clauses.append('(captured<? OR (captured=? AND asset>?))')
            params.extend([after[0], after[0], after[1]])
        where = ' WHERE '+' AND '.join(clauses) if clauses else ''
        rows = self.db.execute('SELECT asset,captured,summary FROM files'+where+' ORDER BY captured DESC,asset LIMIT ?', (*params, limit)).fetchall()
        return dict(items=[json.loads(row['summary']) for row in rows],
                    cursor=[rows[-1]['captured'], rows[-1]['asset']] if rows else None)

    def allowed_paths(self, scope):
        clauses,values=[],[]
        for key,column in [('camera','camera'),('shape','shape'),('kind','kind')]:
            if scope.get(key) is not None:
                clauses.append(column+'=?');values.append(scope[key])
        for key,operator in [('from','>='),('through','<=')]:
            if scope.get(key) is not None:
                clauses.append('captured'+operator+"? AND captured!=''");values.append(scope[key])
        if scope.get('arrivalThrough') is not None:
            clauses.append('arrival<=?');values.append(scope['arrivalThrough'])
        assets=set(scope['assets']) if scope.get('assets') is not None else None
        if not clauses and assets is None:return None
        sql='SELECT asset,path FROM files'+(' WHERE '+' AND '.join(clauses) if clauses else '')
        return {row['path'] for row in self.db.execute(sql,values) if assets is None or row['asset'] in assets}

    def speech(self, query, limit=60, camera=None, day=None, allowed_paths=None, scope=None):
        if not 1 <= limit <= 1000:
            raise ValueError('Result limit must be between 1 and 1000')
        expression = fts_expression(query)
        if not expression:
            return []
        match = 'text : ('+expression+')'
        scope=scope or {}
        camera=scope.get('camera',camera)
        if camera is not None:
            import hashlib
            match += ' AND scope : "c'+hashlib.sha256(camera.encode()).hexdigest()+'"'
        if day is not None:
            datetime.strptime(day, '%Y%m%d')
            match += ' AND scope : "d'+day+'"'
        elif scope.get('from') and scope.get('through'):
            tokens=date_scope_tokens(scope['from'],scope['through'])
            if not tokens:return []
            match+=' AND scope : ('+' OR '.join('"'+token+'"' for token in tokens)+')'
        clauses=[];parameters=[]
        for key,column,operator in [('kind','kind','='),('shape','shape','='),('from','captured','>='),('through','captured','<='),('arrivalThrough','arrival','<=')]:
            if scope.get(key) is not None:
                clauses.append('f.'+column+operator+'?');parameters.append(scope[key])
        assets=set(scope['assets']) if scope.get('assets') is not None else None
        if assets is not None and not assets:return []
        hits, seen = [], set()
        sql='SELECT s.asset,s.start,s.text,f.path,f.kind FROM speech_fts JOIN speech s ON s.id=speech_fts.rowid JOIN files f ON f.asset=s.asset WHERE speech_fts MATCH ?'
        if clauses:sql+=' AND '+' AND '.join(clauses)
        sql+=' ORDER BY speech_fts.rowid DESC'
        with contextlib.closing(self.db.execute(sql, (match,*parameters))) as rows:
            for asset, start, text, path, kind in rows:
                if asset in seen or (allowed_paths is not None and path not in allowed_paths) or (assets is not None and asset not in assets):
                    continue
                seen.add(asset)
                candidates = []
                for operator, direction in [('<=', 'DESC'), ('>', 'ASC')]:
                    candidate = self.db.execute(f'SELECT timestamp,frame,crop FROM frames WHERE asset=? AND timestamp{operator}? ORDER BY timestamp {direction} LIMIT 1', (asset, start)).fetchone()
                    if candidate:
                        candidates.append(candidate)
                frame = min(candidates, key=lambda row: abs(row['timestamp']-start)) if candidates else None
                hits.append(dict(asset=asset,path=path,kind=kind,timestamp=start,evidence=text,frame=frame['frame'] if frame else None,
                                 crop=json.loads(frame['crop'] or 'null') if frame else None))
                if len(hits) >= limit:
                    break
        return hits

    def compact_acknowledged_events(self, catalog):
        """Single-consumer projection: retain the cursor's anchor, drop older events.

        Run only after committed projection updates. A lagging second projection
        detects the missing anchor and must reseed; it cannot silently skip data.
        """
        checkpoint = self.db.execute('SELECT * FROM checkpoint WHERE id=1').fetchone()
        if checkpoint is None or not checkpoint['ready'] or not checkpoint['sequence']:
            return 0
        with catalog_transaction(catalog) as source:
            epoch = source.execute('SELECT epoch FROM search_log_info WHERE id=1').fetchone()[0]
            anchor = source.execute('SELECT token FROM search_events WHERE sequence=?', (checkpoint['sequence'],)).fetchone()
            if epoch != checkpoint['epoch'] or anchor is None or anchor[0] != checkpoint['anchor']:
                raise ValueError('History changed before compaction')
            return source.execute('DELETE FROM search_events WHERE sequence<?', (checkpoint['sequence'],)).rowcount
