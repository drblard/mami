"""Read-only SHA-256 catalog; new outputs and per-file checkpoints only."""
import argparse
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path


def fingerprint(path):
    before = path.stat()
    digest = hashlib.sha256()
    with path.open('rb') as source:
        while chunk := source.read(8 * 1024 * 1024):
            digest.update(chunk)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise RuntimeError('File changed while being fingerprinted')
    return digest.hexdigest(), after


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--inventory', required=True)
    args = parser.parse_args()
    started = time.monotonic()
    inventory = json.loads(Path(args.inventory).read_text())
    root = Path(inventory['root'])
    output = Path.home() / 'mami-lab/catalog' / ('fingerprints-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    output.mkdir(parents=True, exist_ok=False)
    print('CATALOG', output, flush=True)
    paths, failures, groups = {}, [], {}
    total = 0
    for index, record in enumerate(inventory['files']):
        path = root / record['path']
        try:
            digest, stat = fingerprint(path)
            asset = 'sha256:' + digest
            entry = {'path': str(path), 'asset': asset, 'bytes': stat.st_size, 'mtime_ns': stat.st_mtime_ns}
            paths[str(path)] = asset
            groups.setdefault(asset, []).append(str(path))
            total += stat.st_size
        except Exception as exc:
            entry = {'path': str(path), 'error': str(exc)}
            failures.append(entry)
        with (output / f'{index:04d}.json').open('x') as target:
            json.dump(entry, target)
        print(f"{index + 1}/{len(inventory['files'])} files; {total / 1e9:.2f} GB; {len(failures)} failures", flush=True)
    summary = {'paths': paths, 'duplicate_groups': [v for v in groups.values() if len(v) > 1],
               'failures': failures, 'bytes': total, 'elapsed_seconds': time.monotonic() - started}
    with (output / 'catalog.json').open('x') as target:
        json.dump(summary, target, indent=2)
    print(json.dumps({k: v for k, v in summary.items() if k != 'paths'}), flush=True)
    if failures:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
