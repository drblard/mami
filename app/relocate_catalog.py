"""Match moved originals to existing index keys using full SHA-256 fingerprints.

Reads originals only; writes a new report under mami-lab/catalog. Identical
copies are reported, with an ISO-date folder preferred as the playback location.
"""
import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from build_catalog import fingerprint


def preferred_path(paths):
    def rank(path):
        p = Path(path)
        return (not bool(re.fullmatch(r'\d{4}-\d{2}-\d{2}', p.parent.name)), str(p))
    return min(paths, key=rank)


def match_paths(previous, discovered):
    by_asset = defaultdict(list)
    for path, asset in discovered.items():
        by_asset[asset].append(path)
    locations, missing = {}, []
    for old, asset in previous.items():
        if asset in by_asset:
            locations[old] = preferred_path(by_asset[asset])
        else:
            missing.append(old)
    return locations, missing, [sorted(paths) for paths in by_asset.values() if len(paths) > 1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', required=True)
    parser.add_argument('--root', required=True)
    args = parser.parse_args()
    previous = json.loads(Path(args.catalog).read_text())['paths']
    root = Path(args.root).resolve()
    output = Path.home() / 'mami-lab/catalog' / ('relocation-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    output.mkdir(parents=True, exist_ok=False)
    print('RELOCATION', output, flush=True)
    files = sorted(p for p in root.rglob('*') if p.is_file() and p.suffix.lower() in {'.mp4', '.mov', '.jpg', '.jpeg', '.heic', '.png'})
    paths, failures = {}, []
    for i, path in enumerate(files):
        try:
            digest, stat = fingerprint(path)
            paths[str(path)] = 'sha256:' + digest
            record = dict(path=str(path), asset=paths[str(path)], bytes=stat.st_size, mtime_ns=stat.st_mtime_ns)
        except Exception as error:
            record = dict(path=str(path), error=str(error))
            failures.append(record)
        with (output / f'{i:04d}.json').open('x') as target:
            json.dump(record, target)
    locations, missing, duplicates = match_paths(previous, paths)
    known = set(previous.values())
    report = dict(root=str(root), locations=locations, paths={**previous, **paths},
                  missing=missing, duplicate_groups=duplicates, failures=failures,
                  new_files=[path for path, asset in paths.items() if asset not in known])
    with (output / 'relocations.json').open('x') as target:
        json.dump(report, target, indent=2)
    print(json.dumps(dict(scanned=len(files), matched=len(locations), missing=missing,
                         duplicate_groups=len(duplicates), new_files=report['new_files'], failures=failures)), flush=True)
    if missing or failures:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
