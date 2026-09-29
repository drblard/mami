"""Activate a tested immutable bundle, with a verified personal-state audit."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

PERSONAL_TABLES=('annotations','annotation_history','imported_events','clip_selection','photos_import_history','media_roots')


def inspect_personal(path):
    with contextlib.closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.execute('BEGIN')
        if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Personal store integrity failure')
        tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if db.execute('PRAGMA user_version').fetchone()[0]!=1 or not {'annotations','annotation_history','imported_events','user_store_info'}.issubset(tables):
            raise ValueError('Unsupported or incomplete authoritative personal store')
        result={}
        for table in PERSONAL_TABLES:
            if table not in tables:continue
            rows=sorted(db.execute('SELECT * FROM '+table).fetchall(),key=repr)
            result[table]=dict(rows=len(rows),sha256=hashlib.sha256(repr(rows).encode()).hexdigest())
        return result


def processes():
    result=[]
    for line in subprocess.check_output(['ps','-axo','pid,ppid,args'],text=True).splitlines()[1:]:
        parts=line.strip().split(None,2)
        if len(parts)==3:result.append((int(parts[0]),int(parts[1]),parts[2]))
    return result


def point(link,bundle):
    link.parent.mkdir(parents=True,exist_ok=True)
    if link.exists() and not link.is_symlink():raise ValueError('Application link would replace an unfamiliar file/directory')
    temporary=link.with_name(link.name+'.next')
    temporary.symlink_to(bundle)
    os.replace(temporary,link)
    descriptor=os.open(link.parent,os.O_RDONLY)
    try:os.fsync(descriptor)
    finally:os.close(descriptor)


def is_app(command,bundle):
    return '--' not in command and Path(command.split(' ',1)[0]).resolve()==(bundle/'Contents/MacOS/Mami').resolve()


def main(args):
    args.output.mkdir(parents=True,exist_ok=False)
    subprocess.run(['codesign','--verify','--strict',str(args.bundle)],check=True)
    subprocess.run(['codesign','--verify','--strict',str(args.fallback)],check=True)
    configuration=json.loads((args.bundle/'Contents/Resources/configuration.json').read_text())
    if not configuration.get('search_projection') or not configuration.get('packed_index'):
        raise ValueError('Release is missing persistent search configuration')
    before_processes=processes()
    old={pid for pid,_,command in before_processes if is_app(command,args.previous)}
    owned=set(old)
    while True:
        expanded=owned|{pid for pid,parent,_ in before_processes if parent in owned}
        if expanded==owned:break
        owned=expanded
    if any(('import_media.py' in command or 'photos_batch.py' in command) and pid in owned for pid,_,command in before_processes):
        raise RuntimeError('An import is still running; finish it before activation')
    for pid in old:
        subprocess.run(['osascript','-l','JavaScript','-e',f'ObjC.import("AppKit"); $.NSRunningApplication.runningApplicationWithProcessIdentifier({pid}).terminate;'],check=True)
    deadline=time.monotonic()+40
    while owned & {pid for pid,_,_ in processes()}:
        if time.monotonic()>deadline:raise TimeoutError('Old application/workers did not exit')
        time.sleep(.1)
    personal=args.catalog/'user.sqlite'
    before=inspect_personal(personal)
    backup=args.output/'user-before.sqlite'
    with contextlib.closing(sqlite3.connect(personal.resolve().as_uri()+'?mode=ro',uri=True)) as source:
        with contextlib.closing(sqlite3.connect(backup)) as target:source.backup(target)
    if inspect_personal(backup)!=before:raise ValueError('Personal safety backup differs')
    with backup.open('rb') as file:os.fsync(file.fileno())
    descriptor=os.open(args.output,os.O_RDONLY)
    try:os.fsync(descriptor)
    finally:os.close(descriptor)
    (args.output/'personal-before.json').write_text(json.dumps(before,indent=2))
    activated=False
    try:
        resources=args.bundle/'Contents/Resources'
        sys.path.insert(0,str(resources))
        if configuration.get('pack_previews')=='1':
            from preview_atlas import ensure_pack_schema
            ensure_pack_schema(args.catalog/'catalog.sqlite')
        command=[sys.executable,'-B',str(resources/'search_sync.py'),'--catalog',str(args.catalog/'catalog.sqlite'),
                 '--output',configuration['search_projection'],'--index',configuration['index'],
                 '--vector-root',configuration['packed_index'],'--once']
        if configuration.get('speech'):command+=['--speech',configuration['speech']]
        with (args.output/'prepare.log').open('x') as log:
            subprocess.run(command,stdout=log,stderr=log,check=True,timeout=180)
            subprocess.run([sys.executable,'-B',str(resources/'vector_sync.py'),'--projection',configuration['search_projection'],
                            '--output',configuration['packed_index'],'--once'],stdout=log,stderr=log,check=True,timeout=180)
        with contextlib.closing(sqlite3.connect(args.catalog/'catalog.sqlite')) as db:
            count=db.execute('SELECT count(DISTINCT asset) FROM media').fetchone()[0]
            identity=db.execute('SELECT identity FROM state WHERE id=1').fetchone()[0]
        with contextlib.closing(sqlite3.connect(configuration['search_projection'])) as db:
            projected=db.execute('SELECT count(*) FROM files').fetchone()[0]
            if projected!=count or db.execute('SELECT identity FROM checkpoint').fetchone()[0]!=identity:
                raise ValueError('Prepared projection does not match the live catalog')
        if inspect_personal(personal)!=before:raise ValueError('Generated maintenance changed personal state')
        point(args.link,args.bundle)
        subprocess.run(['open','-n',str(args.link)],check=True)
        activated=True
        deadline=time.monotonic()+60
        while True:
            current=processes()
            apps=[pid for pid,_,command in current if is_app(command,args.bundle)]
            descendants=set(apps)
            while True:
                expanded=descendants|{pid for pid,parent,_ in current if parent in descendants}
                if expanded==descendants:break
                descendants=expanded
            roles={name:sum(pid in descendants and name in command for pid,_,command in current) for name in
                   ('search_worker.py','search_sync.py','vector_sync.py','preview_cache_worker.py','index_worker.py')}
            if apps and roles['search_worker.py']>=2 and all(roles[name]>=1 for name in roles if name!='search_worker.py') and roles['index_worker.py']>=2:break
            if time.monotonic()>deadline:raise TimeoutError('Release did not start its independent workers')
            time.sleep(.25)
        with contextlib.closing(sqlite3.connect(personal.resolve().as_uri()+'?mode=ro',uri=True)) as db:
            db.execute('ATTACH DATABASE ? AS prior',(str(backup),))
            missing={table:db.execute(f'SELECT count(*) FROM (SELECT * FROM prior.{table} EXCEPT SELECT * FROM main.{table})').fetchone()[0] for table in before}
        if any(missing.values()):raise ValueError('Release altered or lost existing personal records')
        subprocess.run(['codesign','--verify','--strict',str(args.bundle)],check=True)
        report=dict(status='activated',bundle=str(args.bundle),fallback=str(args.fallback),link=str(args.link),revision=args.revision,
                    catalog_files=count,projected_files=projected,app_pids=apps,workers=roles,
                    personal_before=before,personal_after=inspect_personal(personal),missing_personal_rows=missing)
        (args.output/'result.json').write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2),flush=True)
    except Exception:
        if activated:
            for pid,_,command in processes():
                if is_app(command,args.bundle):
                    subprocess.run(['osascript','-l','JavaScript','-e',f'ObjC.import("AppKit"); $.NSRunningApplication.runningApplicationWithProcessIdentifier({pid}).terminate;'])
            point(args.link,args.fallback)
            subprocess.run(['open','-n',str(args.link)])
        else:subprocess.run(['open','-n',str(args.previous)])
        raise


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('bundle','fallback','previous','catalog','link','output'):parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--revision',required=True)
    main(parser.parse_args())
