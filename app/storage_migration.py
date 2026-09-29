"""Verified, restart-safe publication of production storage outside a development tree.

Originals are never moved or deleted. The source tree remains a rollback copy.
Run with the old app/workers stopped; catalog.lock also excludes cooperating writers.
"""
import argparse
import contextlib
import ctypes
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import uuid

from model_config import MODELS

PERSONAL_TABLES=('annotations','annotation_history','imported_events','clip_selection','photos_import_history','media_roots')


def digest(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def sync_directory(path):
    descriptor=os.open(path,os.O_RDONLY)
    try:os.fsync(descriptor)
    finally:os.close(descriptor)


def write_json(path,value):
    temporary=path.with_name(path.name+'.next')
    with temporary.open('x') as stream:
        json.dump(value,stream,indent=2);stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,path);sync_directory(path.parent)


def publish_directory(source,target):
    """Atomic no-clobber publication, including a destination-created race."""
    library=ctypes.CDLL(None,use_errno=True)
    if sys.platform=='darwin':
        result=library.renamex_np(os.fsencode(source),os.fsencode(target),4)  # RENAME_EXCL
    else:
        result=library.renameat2(-100,os.fsencode(source),-100,os.fsencode(target),1)  # RENAME_NOREPLACE
    if result:raise OSError(ctypes.get_errno(),'Cannot publish migration directory',str(target))
    sync_directory(target.parent)


def copy_verified(source,target):
    source=Path(source);target=Path(target)
    before=source.stat()
    target.parent.mkdir(parents=True,exist_ok=True)
    if target.exists():
        if digest(source)!=digest(target):raise ValueError('Conflicting migration destination: '+str(target))
        return str(target)
    shutil.copy2(source,target)
    if digest(source)!=digest(target) or (source.stat().st_size,source.stat().st_mtime_ns)!=(before.st_size,before.st_mtime_ns):
        raise ValueError('Source changed or copy verification failed: '+str(source))
    with target.open('rb') as stream:os.fsync(stream.fileno())
    return str(target)


def snapshot(source,target):
    target.parent.mkdir(parents=True,exist_ok=True)
    with contextlib.closing(sqlite3.connect(Path(source).resolve().as_uri()+'?mode=ro',uri=True)) as original:
        with contextlib.closing(sqlite3.connect(target)) as destination:
            original.backup(destination)
            if destination.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Snapshot integrity failure')
    with target.open('rb') as stream:os.fsync(stream.fileno())


class Relocator:
    def __init__(self,lab,target,staging,configuration):
        self.lab,self.target,self.staging=lab,target,staging
        self.prefixes=[(lab/'index-artifacts',target/'Derived/Artifacts'),
                       (Path(configuration['index']),target/'Derived/Legacy/Visual'),
                       (Path(configuration['speech']),target/'Derived/Legacy/Speech'),
                       (lab/'runs',target/'Derived/Legacy/Artifacts')]
        self.files={}

    def physical(self,value,required=True):
        if not isinstance(value,str) or not Path(value).is_absolute():return value
        source=Path(value)
        if not source.is_relative_to(self.lab):return value
        if value in self.files:return self.files[value]
        for old,new in self.prefixes:
            if source.is_relative_to(old):
                destination=new/source.relative_to(old)
                staged=self.staging/destination.relative_to(self.target)
                if required and not staged.is_file():
                    if not source.is_file():raise FileNotFoundError('Required derived input is missing: '+value)
                    copy_verified(source,staged)
                self.files[value]=str(destination)
                return str(destination)
        raise ValueError('Unclassified development-tree reference: '+value)

    def payload(self,value,key='',optional_frames=False):
        if isinstance(value,dict):return {k:self.payload(v,k,optional_frames) for k,v in value.items()}
        if isinstance(value,list):return [self.payload(v,key,optional_frames) for v in value]
        if key in ('frame','vector','audio') and isinstance(value,str):return self.physical(value,not (key=='frame' and optional_frames))
        return value


def migrate(lab,target,configuration,checkpoint=lambda stage:None):
    lab=Path(lab).resolve();target=Path(target).absolute()
    if target.is_relative_to(lab):raise ValueError('Production storage must be outside the lab')
    if target.exists():
        receipt=target/'Personal/migration.json'
        if receipt.is_file() and json.loads(receipt.read_text()).get('source')==str(lab):
            value=json.loads(receipt.read_text())
            personal=target/'Personal/user.sqlite'
            if not personal.is_file():raise FileNotFoundError('Published personal database is missing; restore its backup')
            with contextlib.closing(sqlite3.connect(personal.as_uri()+'?mode=ro',uri=True)) as db:
                if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Published personal database is corrupt')
                if db.execute('SELECT source_identity FROM user_store_info WHERE version=1').fetchone()!=(value['identity'],):
                    raise ValueError('Published personal store belongs to another library')
            return value
        raise FileExistsError('Destination already contains unfamiliar data: '+str(target))
    target.parent.mkdir(parents=True,exist_ok=True)
    source=lab/'catalog/database'
    if not (source/'user.sqlite').is_file():raise FileNotFoundError('Authoritative user.sqlite is missing; restore it first')
    with (source/'catalog.lock').open('a+b') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        staging=target.with_name(target.name+'.migrating-'+uuid.uuid4().hex)
        staging.mkdir()
        write_json(staging/'migration-owner.json',dict(source=str(lab),destination=str(target),version=1))
        try:
            personal=staging/'Personal';personal.mkdir(mode=0o700)
            catalog=staging/'Derived/Catalog/catalog.sqlite'
            snapshot(source/'user.sqlite',personal/'user.sqlite')
            (personal/'user.sqlite').chmod(0o600)
            snapshot(source/'catalog.sqlite',catalog)
            with contextlib.closing(sqlite3.connect(personal/'user.sqlite')) as db:
                identity=db.execute('SELECT source_identity FROM user_store_info WHERE version=1').fetchone()[0]
                if db.execute('SELECT identity FROM state').fetchone()[0]!=identity:raise ValueError('Personal identity mismatch')
                before={table:db.execute('SELECT * FROM '+table+' ORDER BY 1').fetchall() for table in PERSONAL_TABLES
                        if db.execute('SELECT 1 FROM sqlite_master WHERE name=?',(table,)).fetchone()}
                personal_state=db.execute('SELECT revision,change_token FROM state WHERE id=1').fetchone()
            with contextlib.closing(sqlite3.connect(catalog)) as db:
                if db.execute('SELECT identity FROM state').fetchone()[0]!=identity:raise ValueError('Catalog belongs to another personal store')
            checkpoint('snapshots')
            backups=lab/'backups/catalog/user-state'
            if backups.exists():shutil.copytree(backups,personal/'Backups/user-state',copy_function=copy_verified)
            annotations=lab/'catalog/annotations'
            if annotations.exists():shutil.copytree(annotations,personal/'LegacyAnnotations',copy_function=copy_verified)
            relocator=Relocator(lab,target,staging,configuration)
            for old,new in relocator.prefixes[:3]:
                if old.exists():shutil.copytree(old,staging/new.relative_to(target),copy_function=copy_verified)
            checkpoint('artifacts')
            with contextlib.closing(sqlite3.connect(catalog)) as db,db:
                db.execute('BEGIN IMMEDIATE')
                maintenance_triggers=db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name GLOB 'preview_pack_*'").fetchall()
                for name,_ in maintenance_triggers:
                    db.execute('DROP TRIGGER "'+name.replace('"','""')+'"')
                # Frozen legacy personal mirrors are not authoritative or read by
                # current workers. Preserve their bytes; only active derived payloads change.
                for table in ('media','index_units'):
                    for rowid,payload in db.execute('SELECT rowid,payload FROM '+table).fetchall():
                        updated=json.dumps(relocator.payload(json.loads(payload)),sort_keys=True)
                        db.execute('UPDATE '+table+' SET payload=? WHERE rowid=?',(updated,rowid))
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='seed_sources'").fetchone():
                    for (value,) in db.execute('SELECT source FROM seed_sources').fetchall():
                        for old,new in relocator.prefixes[:3]:
                            if value.startswith(str(old)+':'):
                                db.execute('UPDATE seed_sources SET source=? WHERE source=?',(str(new)+value[len(str(old)):],value))
                                break
                for table,columns in [('preview_packs',('directory',)),('preview_retired',('directory',)),
                                      ('preview_retire_pending',('directory',)),('preview_generations',('directory','staging'))]:
                    if not db.execute('SELECT 1 FROM sqlite_master WHERE name=?',(table,)).fetchone():continue
                    for column in columns:
                        for rowid,value in db.execute('SELECT rowid,'+column+' FROM '+table).fetchall():
                            db.execute('UPDATE '+table+' SET '+column+'=? WHERE rowid=?',(relocator.physical(value,False),rowid))
                for _,sql in maintenance_triggers:db.execute(sql)
            for name in ('Visual','Speech'):
                directory=staging/'Derived/Legacy'/name
                for path in directory.rglob('*.json'):
                    value=json.loads(path.read_text())
                    path.write_text(json.dumps(relocator.payload(value),ensure_ascii=False))
            # Rewrite atlas descriptors and their registered manifest digests together.
            with contextlib.closing(sqlite3.connect(catalog)) as db,db:
                for path in (staging/'Derived/Artifacts/packed-previews').glob('*/manifest.json'):
                    value=relocator.payload(json.loads(path.read_text()))
                    value['originals']=[relocator.physical(p,False) for p in value.get('originals',[])]
                    value['original_signatures']={relocator.physical(p,False):v for p,v in value.get('original_signatures',{}).items()}
                    path.write_text(json.dumps(value,indent=2))
                    if db.execute("SELECT 1 FROM sqlite_master WHERE name='preview_generations'").fetchone():
                        directory=str(target/path.parent.relative_to(staging))
                        db.execute('UPDATE preview_generations SET digest=? WHERE directory=?',(digest(path),directory))
            with contextlib.closing(sqlite3.connect(personal/'user.sqlite')) as db,db:
                # Cached selection thumbnails move; content IDs/order/times and all
                # other personal records must remain exactly unchanged.
                if 'clip_selection' in before:
                    for rowid,payload in db.execute('SELECT rowid,payload FROM clip_selection').fetchall():
                        original=json.loads(payload)
                        updated=relocator.payload(original,optional_frames=True)
                        def semantic(value):
                            if isinstance(value,dict):return {k:semantic(v) for k,v in value.items() if k!='frame'}
                            if isinstance(value,list):return [semantic(v) for v in value]
                            return value
                        if semantic(original)!=semantic(updated):raise ValueError('Selection meaning changed')
                        if updated!=json.loads(payload):db.execute('UPDATE clip_selection SET payload=? WHERE rowid=?',(json.dumps(updated),rowid))
                db.execute('UPDATE state SET revision=?,change_token=? WHERE id=1',personal_state)
                for table,rows in before.items():
                    current=db.execute('SELECT * FROM '+table+' ORDER BY 1').fetchall()
                    if table!='clip_selection' and current!=rows:raise ValueError('Personal records changed: '+table)
            checkpoint('references')
            for repository,revision in MODELS.values():
                relative=Path('huggingface/hub')/('models--'+repository.replace('/','--'))/'snapshots'/revision
                source_model=lab/'cache'/relative
                if not source_model.is_dir():raise FileNotFoundError('Required pinned model missing: '+repository)
                shutil.copytree(source_model,staging/'Models'/relative,copy_function=copy_verified)
            checkpoint('models')
            for path in staging.rglob('*'):
                if path.is_file():
                    with path.open('rb') as stream:os.fsync(stream.fileno())
            for path in sorted((p for p in staging.rglob('*') if p.is_dir()),key=lambda p:len(p.parts),reverse=True):sync_directory(path)
            write_json(personal/'ownership.json',dict(version=1,identity=identity))
            receipt=dict(version=1,source=str(lab),identity=identity,phase='data-published',
                         personal_counts={table:len(rows) for table,rows in before.items()},relocated_files=len(relocator.files))
            write_json(personal/'migration.json',receipt)
            checkpoint('publication')
            publish_directory(staging,target)
            return receipt
        except BaseException:
            # Preserve the explicitly marked staging tree for diagnosis/resume;
            # never touch the source or overwrite an existing destination.
            raise


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lab',type=Path,required=True)
    parser.add_argument('--target',type=Path,required=True)
    parser.add_argument('--configuration',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(migrate(args.lab,args.target,json.loads(args.configuration.read_text())),indent=2))
