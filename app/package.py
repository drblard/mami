"""Run on the Mac in a freshly deployed, compiled source directory."""
import argparse
import json
import plistlib
import shutil
import subprocess
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--index', required=True)
parser.add_argument('--speech')
parser.add_argument('--inventory')
parser.add_argument('--catalog')
parser.add_argument('--metadata', help='Reuse previously extracted capture metadata for unchanged media')
parser.add_argument('--relocations', help='Verified SHA-256 relocation report')
args = parser.parse_args()
root = Path(__file__).resolve().parent
bundle = root / 'Mami.app'
bundle.mkdir(exist_ok=False)
contents = bundle / 'Contents'
executable = contents / 'MacOS'
resources = contents / 'Resources'
executable.mkdir(parents=True)
resources.mkdir()
shutil.copy2(root / '.build/release/Mami', executable / 'Mami')
for name in ['search_worker.py', 'lab.py', 'index_worker.py', 'index_queue.py', 'index_backend.py', 'metadata.py', 'import_media.py', 'gpu_activity.py', 'photos_batch.py']:
    shutil.copy2(root / name, resources / name)
with (resources / 'configuration.json').open('x') as f:
    config = {'index': args.index}
    if args.speech:
        config['speech'] = args.speech
    json.dump(config, f)
if args.inventory:
    from metadata import export_metadata
    metadata = export_metadata(args.inventory)
    with (resources / 'metadata.json').open('x') as f:
        json.dump(metadata, f, ensure_ascii=False)
    print('METADATA', len(metadata), 'locations', sum(bool(m['location']) for m in metadata.values()),
          'errors', sum('error' in m for m in metadata.values()))
elif args.metadata:
    shutil.copy2(args.metadata, resources / 'metadata.json')
if args.relocations:
    relocations = json.loads(Path(args.relocations).read_text())
    if relocations['missing'] or relocations['failures']:
        raise ValueError('Cannot package an incomplete relocation report')
    with (resources / 'relocations.json').open('x') as f:
        json.dump(relocations['locations'], f)
    with (resources / 'identities.json').open('x') as f:
        json.dump(relocations['paths'], f)
elif args.catalog:
    catalog = json.loads(Path(args.catalog).read_text())
    if catalog['failures']:
        raise ValueError('Cannot package an incomplete content catalog')
    with (resources / 'identities.json').open('x') as f:
        json.dump(catalog['paths'], f)
with (contents / 'Info.plist').open('xb') as f:
    plistlib.dump({'CFBundleExecutable': 'Mami', 'CFBundleIdentifier': 'local.mami.prototype',
                  'CFBundleName': 'Mami', 'CFBundleDisplayName': 'Mami',
                  'CFBundlePackageType': 'APPL', 'CFBundleVersion': '1',
                  'CFBundleShortVersionString': '0.1.0', 'LSMinimumSystemVersion': '14.0',
                  'NSHighResolutionCapable': True,
                  'NSPhotoLibraryUsageDescription': 'Mami imports full originals from your synced Photos library into independent, verified local copies. Mami never deletes from Photos or iCloud.'}, f)
subprocess.run(['/usr/bin/codesign', '--sign', '-', str(bundle)], check=True)
print(bundle)
