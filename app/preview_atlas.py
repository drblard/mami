"""Pack completed preview frames into immutable JPEG sheets with crop metadata."""
import json
import hashlib
import os
from pathlib import Path
import tempfile
import uuid

from index_store import connection,ensure_schema,signature
from model_config import PIPELINE

CELL_PIXELS = 256
SHEET_COLUMNS = 8
FRAMES_PER_SHEET = 32
JPEG_QUALITY = 80
ATLAS_VERSION = 1


def ensure_pack_schema(database):
    with connection(database) as db:
        ensure_schema(db)
        db.execute('CREATE TABLE IF NOT EXISTS preview_packs(asset TEXT PRIMARY KEY,version INTEGER NOT NULL,directory TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS preview_pack_errors(asset TEXT PRIMARY KEY,error TEXT NOT NULL,attempts INTEGER NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS preview_retired(asset TEXT PRIMARY KEY,directory TEXT NOT NULL)')
        db.execute("CREATE TRIGGER IF NOT EXISTS preview_pack_frame_update AFTER UPDATE ON index_units WHEN NEW.stage='frame' BEGIN DELETE FROM preview_packs WHERE asset=NEW.asset; DELETE FROM preview_pack_errors WHERE asset=NEW.asset; END")
        db.execute("CREATE TRIGGER IF NOT EXISTS preview_pack_frame_delete AFTER DELETE ON index_units WHEN OLD.stage='frame' BEGIN DELETE FROM preview_packs WHERE asset=OLD.asset; END")


def pack_asset(database, asset, artifacts, checkpoint=lambda: None):
    from PIL import Image, ImageOps
    ensure_pack_schema(database)
    with connection(database) as db:
        job = db.execute('SELECT state FROM index_jobs WHERE asset=?', (asset,)).fetchone()
        if job is None or job['state'] != 'complete':
            return None
        rows = db.execute("SELECT ordinal,payload FROM index_units WHERE asset=? AND pipeline=? AND stage='frame' ORDER BY ordinal", (asset, PIPELINE)).fetchall()
        frames = [(row['ordinal'], json.loads(row['payload'])) for row in rows]
        if len(frames) < 2:
            return None
        if all(sample.get('crop') for _, sample in frames):
            db.execute('INSERT OR REPLACE INTO preview_packs VALUES(?,?,?)',(asset,ATLAS_VERSION,str(Path(frames[0][1]['frame']).parent)))
            return None
        original_payloads = [(row['ordinal'], row['payload']) for row in rows]
    root = Path(artifacts)/'packed-previews'
    root.mkdir(parents=True, exist_ok=True)
    final = root/uuid.uuid4().hex
    updated = {}
    original_files = []
    original_signatures = {}
    sheets = {}
    with tempfile.TemporaryDirectory(prefix='.packing-', dir=root) as staging:
        staging = Path(staging)
        for start in range(0, len(frames), FRAMES_PER_SHEET):
            checkpoint()
            group = frames[start:start+FRAMES_PER_SHEET]
            columns = min(SHEET_COLUMNS, len(group))
            height = ((len(group)+columns-1)//columns)*CELL_PIXELS
            sheet = Image.new('RGB', (columns*CELL_PIXELS, height), (16,16,16))
            name = f'{start//FRAMES_PER_SHEET:06d}.jpg'
            for index, (ordinal, sample) in enumerate(group):
                original_signatures[sample['frame']] = signature(Path(sample['frame']))
                with Image.open(sample['frame']) as image:
                    if sample.get('crop'):
                        x,y,width,height=sample['crop']
                        if x<0 or y<0 or width<=0 or height<=0 or x+width>image.width or y+height>image.height:
                            raise ValueError('Invalid existing preview crop')
                        image=image.crop((x,y,x+width,y+height))
                    source_size=sample.get('sourceSize',list(image.size))
                    thumbnail = ImageOps.contain(ImageOps.exif_transpose(image).convert('RGB'), (CELL_PIXELS,CELL_PIXELS), Image.Resampling.LANCZOS)
                x = index%columns*CELL_PIXELS+(CELL_PIXELS-thumbnail.width)//2
                y = index//columns*CELL_PIXELS+(CELL_PIXELS-thumbnail.height)//2
                sheet.paste(thumbnail, (x,y))
                updated[ordinal] = dict(sample, frame=str(final/name), crop=[x,y,thumbnail.width,thumbnail.height],sourceSize=source_size)
                original_files.append(sample['frame'])
            sheet.save(staging/name, quality=JPEG_QUALITY, optimize=True)
            with (staging/name).open('rb') as file:
                os.fsync(file.fileno())
                sheets[name] = hashlib.file_digest(file,'sha256').hexdigest()
        manifest = dict(version=ATLAS_VERSION, asset=asset, pipeline=PIPELINE,
                        frames=[dict(ordinal=ordinal, sample=updated[ordinal]) for ordinal,_ in frames],
                        originals=original_files)
        manifest['original_signatures'] = original_signatures
        manifest['sheets'] = sheets
        with (staging/'manifest.json').open('x') as file:
            json.dump(manifest,file,indent=2);file.flush();os.fsync(file.fileno())
        checkpoint()
        with connection(database) as db:
            current = db.execute("SELECT ordinal,payload FROM index_units WHERE asset=? AND pipeline=? AND stage='frame' ORDER BY ordinal", (asset,PIPELINE)).fetchall()
            state = db.execute('SELECT state FROM index_jobs WHERE asset=?', (asset,)).fetchone()
            if state is None or state['state'] != 'complete' or [(row['ordinal'],row['payload']) for row in current] != original_payloads:
                return None
            os.rename(staging, final)
            descriptor = os.open(root,os.O_RDONLY)
            try:os.fsync(descriptor)
            finally:os.close(descriptor)
            replacements = {(sample['frame'],sample.get('timestamp')):updated[ordinal] for ordinal,sample in frames}
            for ordinal, sample in updated.items():
                db.execute("UPDATE index_units SET payload=? WHERE asset=? AND pipeline=? AND stage='frame' AND ordinal=?",
                           (json.dumps(sample,sort_keys=True),asset,PIPELINE,ordinal))
                row = db.execute("SELECT payload FROM index_units WHERE asset=? AND pipeline=? AND stage='embedding' AND ordinal=?", (asset,PIPELINE,ordinal)).fetchone()
                if row:
                    value = json.loads(row['payload'])
                    value['sample'] = sample
                    db.execute("UPDATE index_units SET payload=? WHERE asset=? AND pipeline=? AND stage='embedding' AND ordinal=?",
                               (json.dumps(value,sort_keys=True),asset,PIPELINE,ordinal))
            for row in db.execute('SELECT path,payload FROM media WHERE asset=?',(asset,)).fetchall():
                value=json.loads(row['payload'])
                value['frames']=[replacements.get((sample['frame'],sample.get('timestamp')),sample) for sample in value['frames']]
                sample=value['match']
                value['match']=replacements.get((sample['frame'],sample.get('timestamp')),sample)
                db.execute('UPDATE media SET payload=? WHERE path=?',(json.dumps(value,sort_keys=True),row['path']))
            db.execute('CREATE TABLE IF NOT EXISTS preview_packs(asset TEXT PRIMARY KEY,version INTEGER NOT NULL,directory TEXT NOT NULL)')
            db.execute('INSERT INTO preview_packs VALUES(?,?,?) ON CONFLICT(asset) DO UPDATE SET version=excluded.version,directory=excluded.directory',
                       (asset,ATLAS_VERSION,str(final)))
    return manifest


def retire_raw_frames(database,asset,artifacts,projection,limit=256):
    """Retire only unchanged, owned raw JPEGs after packed publication is visible.

    No originals, vectors, legacy experiment frames or unknown files qualify.
    Old packed directories are retained for a separate generation cleanup pass.
    """
    import contextlib
    import sqlite3
    artifacts=Path(artifacts).resolve()
    with connection(database) as db:
        row=db.execute('SELECT directory FROM preview_packs WHERE asset=? AND version=?',(asset,ATLAS_VERSION)).fetchone()
        if not row:return None
        directory=Path(row['directory'])
        if directory.is_symlink() or directory.parent.resolve()!=artifacts/'packed-previews':return None
        manifest=json.loads((directory/'manifest.json').read_text())
        if manifest.get('asset')!=asset or manifest.get('version')!=ATLAS_VERSION:return None
        current=[json.loads(row['payload']) for row in db.execute("SELECT payload FROM index_units WHERE asset=? AND pipeline=? AND stage='frame'",(asset,PIPELINE))]
        targets={str(directory/name.name) for name in directory.glob('*.jpg')}
        if not current or any(frame['frame'] not in targets or not frame.get('crop') for frame in current):return None
        for target in targets:
            expected=manifest.get('sheets',{}).get(Path(target).name)
            if expected is None:return None
            with open(target,'rb') as image:
                if hashlib.file_digest(image,'sha256').hexdigest()!=expected:
                    raise ValueError('Packed preview checksum changed; raw frames retained')
        with contextlib.closing(sqlite3.connect(Path(projection).resolve().as_uri()+'?mode=ro',uri=True)) as visible:
            frames=visible.execute('SELECT frame,crop FROM frames WHERE asset=?',(asset,)).fetchall()
            if len(frames)!=len(current) or any(frame not in targets or not json.loads(crop or 'null') for frame,crop in frames):return None
        folder=artifacts/asset.removeprefix('sha256:')
        removed=0
        for name,saved in manifest.get('original_signatures',{}).items():
            if removed>=limit:break
            path=Path(name)
            if path.is_symlink() or not path.is_file() or path.suffix.lower()!='.jpg':continue
            if path.parent.resolve()!=folder or artifacts not in path.resolve().parents:continue
            if signature(path)!=saved:continue
            path.unlink();removed+=1
        if removed:
            descriptor=os.open(folder,os.O_RDONLY)
            try:os.fsync(descriptor)
            finally:os.close(descriptor)
        return removed
