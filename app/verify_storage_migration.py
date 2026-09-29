"""Read-only verification of migrated personal records and physical media references."""
import argparse
import contextlib
import json
from pathlib import Path
import sqlite3

from storage_migration import PERSONAL_TABLES


def semantic(value):
    if isinstance(value,dict):return {k:semantic(v) for k,v in value.items() if k!='frame'}
    if isinstance(value,list):return [semantic(v) for v in value]
    return value


def verify(lab,target):
    def opened(path):return contextlib.closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True))
    with opened(lab/'catalog/database/user.sqlite') as old,opened(target/'Personal/user.sqlite') as new:
        counts={}
        for table in PERSONAL_TABLES:
            if not old.execute('SELECT 1 FROM sqlite_master WHERE name=?',(table,)).fetchone():continue
            before=old.execute('SELECT * FROM '+table+' ORDER BY 1').fetchall()
            after=new.execute('SELECT * FROM '+table+' ORDER BY 1').fetchall()
            if table=='clip_selection':
                before=[(row[0],semantic(json.loads(row[1]))) for row in before]
                after=[(row[0],semantic(json.loads(row[1]))) for row in after]
            if before!=after:raise ValueError('Personal content differs: '+table)
            counts[table]=len(after)
        if old.execute('SELECT * FROM state').fetchall()!=new.execute('SELECT * FROM state').fetchall():
            raise ValueError('Generated relocation changed personal revision')
        if new.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Personal integrity failure')
    paths=set()
    def collect(value,key=''):
        if isinstance(value,dict):
            for k,v in value.items():collect(v,k)
        elif isinstance(value,list):
            for v in value:collect(v,key)
        elif key in ('frame','vector','audio') and isinstance(value,str):paths.add(value)
    with opened(lab/'catalog/database/catalog.sqlite') as old,opened(target/'Derived/Catalog/catalog.sqlite') as new:
        before={row[0]:(row[1],json.loads(row[2])['url']) for row in old.execute('SELECT path,asset,payload FROM media')}
        after={row[0]:(row[1],json.loads(row[2])['url']) for row in new.execute('SELECT path,asset,payload FROM media')}
        if before!=after:raise ValueError('Original-media locations/identities changed')
        for table in ('media','index_units'):
            for (payload,) in new.execute('SELECT payload FROM '+table):collect(json.loads(payload))
        if new.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Generated catalog integrity failure')
    for value in paths:
        path=Path(value)
        if path.is_relative_to(lab):raise ValueError('Active physical lab reference remains: '+value)
        if not path.is_file():raise FileNotFoundError('Migrated resource missing: '+value)
    return dict(status='passed',personal_counts=counts,media=len(after),physical_references=len(paths),
                original_locations_unchanged=True,personal_revision_unchanged=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lab',type=Path,required=True)
    parser.add_argument('--target',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();result=verify(args.lab,args.target)
    args.output.write_text(json.dumps(result,indent=2));print(json.dumps(result,indent=2))
