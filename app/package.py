"""Run on the Mac in a freshly deployed, compiled source directory."""
import argparse
import json
import plistlib
import shutil
from datetime import datetime, timezone
from pathlib import Path
import signing
from bundle_resources import WORKER_RESOURCES

parser = argparse.ArgumentParser()
parser.add_argument('--index', required=True)
parser.add_argument('--speech')
parser.add_argument('--native-encoder')
parser.add_argument('--packed-index')
parser.add_argument('--search-projection')
parser.add_argument('--pack-previews', action='store_true')
parser.add_argument('--inventory')
parser.add_argument('--catalog')
parser.add_argument('--metadata', help='Reuse previously extracted capture metadata for unchanged media')
parser.add_argument('--relocations', help='Verified SHA-256 relocation report')
args = parser.parse_args()
if args.packed_index and (not args.native_encoder or not args.search_projection):
    raise ValueError('Packed search requires both native encoder and search projection')
if args.pack_previews and not args.packed_index:
    raise ValueError('Preview packing requires persistent search and its crop-aware projection')
signing.load()  # No silent fallback to build-specific ad-hoc signing.
root = Path(__file__).resolve().parent
bundle = root / 'Mami.app'
bundle.mkdir(exist_ok=False)
contents = bundle / 'Contents'
executable = contents / 'MacOS'
resources = contents / 'Resources'
executable.mkdir(parents=True)
resources.mkdir()
shutil.copy2(root / '.build/release/Mami', executable / 'Mami')
for name in WORKER_RESOURCES:
    shutil.copy2(root / name, resources / name)
with (resources / 'configuration.json').open('x') as f:
    config = {'index': args.index}
    if args.speech:
        config['speech'] = args.speech
    if args.native_encoder:
        config['native_encoder'] = args.native_encoder
    if args.packed_index:
        config['packed_index'] = args.packed_index
        config['search_projection'] = args.search_projection
    if args.pack_previews:
        config['pack_previews'] = '1'
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
                  'CFBundlePackageType': 'APPL', 'CFBundleVersion': str(int(datetime.now(timezone.utc).timestamp())),
                  'CFBundleShortVersionString': '0.1.0', 'LSMinimumSystemVersion': '14.0',
                  'NSHighResolutionCapable': True,
                  'NSPhotoLibraryUsageDescription': 'Mami imports full originals from your synced Photos library into independent, verified local copies. Mami never deletes from Photos or iCloud.'}, f)
signing.sign(bundle)
print(bundle)
