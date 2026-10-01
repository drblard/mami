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
parser.add_argument('--index')
parser.add_argument('--standard-layout',action='store_true')
parser.add_argument('--runtime-env',type=Path,help='Build-time Python environment to embed')
parser.add_argument('--runtime-tree',type=Path,help='Previously verified embedded-runtime Contents tree')
parser.add_argument('--ffmpeg',type=Path,help='Build-time FFmpeg executable to embed')
parser.add_argument('--ffprobe',type=Path,help='Build-time ffprobe executable to embed')
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
if not args.standard_layout and not args.index:raise ValueError('Supply an index or use the standard production layout')
if args.runtime_env and args.runtime_tree:raise ValueError('Choose one runtime source')
if args.standard_layout and not (args.native_encoder and (args.runtime_tree or all((args.runtime_env,args.ffmpeg,args.ffprobe)))):
    raise ValueError('A production bundle requires its Python/media runtime and native text encoder')
if args.runtime_env and not all((args.ffmpeg,args.ffprobe)):
    raise ValueError('An embedded runtime also requires both media tools')
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
nested=[];runtime_report=None
if args.runtime_env:
    from bundle_runtime import embed_runtime
    nested,runtime_report=embed_runtime(contents,args.runtime_env,args.ffmpeg,args.ffprobe)
elif args.runtime_tree:
    for name in ('Helpers','Frameworks','Resources/ThirdParty','Resources/Python'):
        source=args.runtime_tree/name
        if source.exists():shutil.copytree(source,contents/name,dirs_exist_ok=True,symlinks=True)
    runtime_report=json.loads((resources/'ThirdParty/runtime-manifest.json').read_text())
    nested=[contents/path for path in [*runtime_report['bundled_libraries'],*runtime_report.get('runtime_bundles',[])]]
    if any(not path.resolve().is_relative_to(contents.resolve()) or not path.exists() for path in nested):
        raise ValueError('Invalid cached runtime manifest')
for name in WORKER_RESOURCES:
    shutil.copy2(root / name, resources / name)
with (resources / 'configuration.json').open('x') as f:
    config = {'layout': 'standard-v1','pack_previews':'1'} if args.standard_layout else {'index': args.index}
    if args.speech and not args.standard_layout:
        config['speech'] = args.speech
    if args.native_encoder and not args.standard_layout:
        config['native_encoder'] = args.native_encoder
    if args.packed_index and not args.standard_layout:
        config['packed_index'] = args.packed_index
        config['search_projection'] = args.search_projection
    if args.pack_previews:
        config['pack_previews'] = '1'
    json.dump(config, f)
if args.standard_layout:
    shutil.copytree(args.native_encoder,resources/'TextEncoder',symlinks=False)
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
                  'CFBundleShortVersionString': '0.2.0', 'LSMinimumSystemVersion': runtime_report['minimum_macos'] if runtime_report else '14.0',
                  'NSHighResolutionCapable': True,
                  # AppKit otherwise swallows main-thread Objective-C exceptions after
                  # unwinding Swift frames, leaving the app running without main-actor work.
                  'NSApplicationCrashOnExceptions': True,
                  'NSPhotoLibraryUsageDescription': 'Mami imports full originals from your synced Photos library into independent, verified local copies. Mami never deletes from Photos or iCloud.'}, f)
signing.sign(bundle,nested=nested)
print(bundle)
