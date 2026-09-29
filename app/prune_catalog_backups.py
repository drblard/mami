"""Plan/verify/prune redundant completed catalog snapshots; preserve manual states.

Unknown, incomplete, unsupported and changed files are never removal candidates.
Default is a dry run. --apply requires a retained plan/output directory and checks
the retained newest snapshots before deleting any candidate.
"""
import argparse
import contextlib
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time


MANUAL_TABLES = ('annotations', 'annotation_history', 'clip_selection', 'media_roots')


def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w') as output:
        json.dump(value, output, indent=2)
        output.flush(); os.fsync(output.fileno())
    os.replace(temporary, path)


def signature(path):
    stat = path.stat()
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]


def inspect(path, integrity=False):
    with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        if db.execute('PRAGMA user_version').fetchone()[0] != 2:
            raise ValueError('Unsupported schema')
        if integrity and db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('Integrity check failed')
        identity, revision, token = db.execute('SELECT identity,revision,change_token FROM state WHERE id=1').fetchone()
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'annotations', 'annotation_history'}.issubset(tables):
            raise ValueError('Missing manual-data tables')
        digest = hashlib.sha256()
        for table in MANUAL_TABLES:
            digest.update(table.encode())
            if table in tables:
                # Schema + sorted values distinguish edits even if counts agree.
                digest.update(repr(db.execute(f'PRAGMA table_info({table})').fetchall()).encode())
                for row in sorted(db.execute(f'SELECT * FROM {table}').fetchall(), key=repr):
                    digest.update(repr(row).encode())
        return dict(identity=identity, revision=str(revision), token=token, manual=digest.hexdigest())


def retained_names(snapshots, now):
    keep = set()
    identities = {}
    for snapshot in snapshots:
        identities.setdefault(snapshot['identity'], []).append(snapshot)
    for group in identities.values():
        group.sort(key=lambda row: (row['mtime'], row['name']), reverse=True)
        keep.update(row['name'] for row in group[:3])
        keep.add(group[-1]['name'])  # Original baseline for this catalog identity.
        states, hours, days, months = set(), set(), set(), set()
        for row in group:
            if row['manual'] not in states:
                keep.add(row['name']); states.add(row['manual'])
            age = max(0, now-row['mtime'])
            date = datetime.fromtimestamp(row['mtime'], timezone.utc)
            bucket = date.strftime('%Y-%m-%d-%H')
            if age <= 24*3600 and bucket not in hours:
                keep.add(row['name']); hours.add(bucket)
            bucket = date.strftime('%Y-%m-%d')
            if age <= 30*86400 and bucket not in days:
                keep.add(row['name']); days.add(bucket)
            bucket = date.strftime('%Y-%m')
            if age <= 366*86400 and bucket not in months:
                keep.add(row['name']); months.add(bucket)
    return keep


def plan(directory, catalog=None, now=None):
    now = time.time() if now is None else now
    snapshots, unknown = [], []
    for receipt in sorted(directory.glob('*.json')):
        try:
            value = json.loads(receipt.read_text())
            name = value['file']
            if Path(name).name != name or not name.startswith('catalog-') or not name.endswith('.sqlite'):
                raise ValueError('Unexpected snapshot filename')
            path = directory/name
            if path.is_symlink() or receipt.is_symlink():
                raise ValueError('Symbolic link')
            before = signature(path)
            state = inspect(path)
            if state['identity'] != value['identity'] or state['revision'] != str(value['revision']) or state['token'] != value['changeToken']:
                raise ValueError('Receipt/state mismatch')
            journal = Path(str(path)+'-journal')
            if journal.exists():
                with journal.open('rb') as source:
                    if any(source.read(8)): raise ValueError('Nonzero journal header')
            if before != signature(path): raise ValueError('Snapshot changed during inspection')
            pending = Path(str(path)+'.receipt-pending')
            if pending.exists() and json.loads(pending.read_text()) != value:
                raise ValueError('Pending receipt mismatch')
            ancillary = [receipt] + ([pending] if pending.exists() else []) + ([journal] if journal.exists() else [])
            snapshots.append(dict(name=name, receipt=receipt.name, signature=before,
                                  mtime=path.stat().st_mtime, bytes=before[2],
                                  ancillary={p.name: signature(p) for p in ancillary}, **state))
        except Exception as error:
            unknown.append(dict(receipt=receipt.name, reason=str(error)))
    keep = retained_names(snapshots, now)
    if catalog:
        # The live catalog is a normal read transaction, never immutable.
        with contextlib.closing(sqlite3.connect(catalog.resolve().as_uri()+'?mode=ro', uri=True, timeout=10)) as db:
            current = db.execute('SELECT identity,revision,change_token FROM state WHERE id=1').fetchone()
        keep.update(row['name'] for row in snapshots if (row['identity'],row['revision'],row['token']) == (current[0],str(current[1]),current[2]))
    return dict(directory=str(directory.resolve()), created=now, snapshots=snapshots,
                keep=sorted(keep), remove=[row['name'] for row in snapshots if row['name'] not in keep], unknown=unknown,
                policy='Newest 3 + original baseline per identity; latest representative of EVERY distinct manual-data state; 24 hourly / 30 daily / 12 monthly windows.')


def apply(value, output):
    directory = Path(value['directory'])
    by_name = {row['name']:row for row in value['snapshots']}
    # Verify all retained manual-state/restore representatives before any removal.
    verified = []
    for name in value['keep']:
        row = by_name[name]
        if signature(directory/name) != row['signature']:
            raise ValueError('Retained snapshot changed: '+name)
        state = inspect(directory/name, integrity=True)
        if any(state[key] != row[key] for key in state):
            raise ValueError('Retained snapshot state changed: '+name)
        verified.append(name)
    atomic_json(output/'verified.json', dict(files=verified, completed=time.time()))
    removed, skipped = [], []
    for name in value['remove']:
        row = by_name[name]
        path = directory/name
        if not path.exists():
            skipped.append(dict(name=name, reason='Already absent')); continue
        if signature(path) != row['signature'] or any(not (directory/n).exists() or signature(directory/n) != sig for n,sig in row['ancillary'].items()):
            skipped.append(dict(name=name, reason='Changed after plan')); continue
        # Remove the database last. An interruption can leave an extra orphan,
        # never a deleted retained restore point. Unrecognized files stay intact.
        for ancillary in row['ancillary']:
            (directory/ancillary).unlink()
        path.unlink()
        removed.append(name)
        if len(removed)%100 == 0:
            atomic_json(output/'progress.json', dict(removed=removed, skipped=skipped))
            print(json.dumps(dict(removed=len(removed),total=len(value['remove']))),flush=True)
    result = dict(removed=len(removed), logical_bytes=sum(by_name[n]['bytes'] for n in removed),
                  retained=len(verified), skipped=skipped, unknown=value['unknown'], completed=time.time())
    atomic_json(output/'result.json', result)
    print(json.dumps(result),flush=True)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backups',type=Path,required=True)
    parser.add_argument('--catalog',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    value=plan(args.backups,args.catalog)
    atomic_json(args.output/'plan.json',value)
    summary=dict(completed_snapshots=len(value['snapshots']),retain=len(value['keep']),remove=len(value['remove']),
                 unknown=len(value['unknown']),logical_bytes_to_remove=sum(r['bytes'] for r in value['snapshots'] if r['name'] in set(value['remove'])))
    print(json.dumps(summary),flush=True)
    if args.apply:apply(value,args.output)
