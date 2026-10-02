"""Assemble and sign Mami.app; run on the Mac in a freshly deployed, compiled snapshot."""
import argparse
import json
import plistlib
import shutil
from datetime import datetime, timezone
from pathlib import Path
import signing
from bundle_resources import WORKER_RESOURCES

RUNTIME_PARTS = ('Helpers', 'Frameworks', 'Resources/ThirdParty', 'Resources/Python')
PHOTOS_USAGE = ('Mami imports full originals from your synced Photos library into independent, verified local copies. '
                'Mami never deletes from Photos or iCloud.')


def parse(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    runtime = parser.add_mutually_exclusive_group(required=True)
    runtime.add_argument('--runtime-tree', type=Path, help='Contents of a verified bundle whose Python/media runtime is reused')
    runtime.add_argument('--runtime-env', type=Path, help='Build-time Python environment to embed (with --ffmpeg/--ffprobe)')
    parser.add_argument('--ffmpeg', type=Path, help='Build-time FFmpeg executable to embed')
    parser.add_argument('--ffprobe', type=Path, help='Build-time ffprobe executable to embed')
    parser.add_argument('--native-encoder', type=Path, required=True, help='Native text-encoder artifact directory')
    parser.add_argument('--revision', default='unknown', help='Source revision recorded in Info.plist')
    args = parser.parse_args(argv)
    if args.runtime_env and not (args.ffmpeg and args.ffprobe):
        parser.error('--runtime-env also requires --ffmpeg and --ffprobe')
    return args


def main(args):
    signing.load()  # No silent fallback to build-specific ad-hoc signing.
    root = Path(__file__).resolve().parent
    bundle = root / 'Mami.app'
    bundle.mkdir(exist_ok=False)
    contents = bundle / 'Contents'
    resources = contents / 'Resources'
    (contents / 'MacOS').mkdir(parents=True)
    resources.mkdir()
    shutil.copy2(root / '.build/release/Mami', contents / 'MacOS/Mami')
    if args.runtime_env:
        from bundle_runtime import embed_runtime
        nested, runtime_report = embed_runtime(contents, args.runtime_env, args.ffmpeg, args.ffprobe)
    else:
        for name in RUNTIME_PARTS:
            source = args.runtime_tree / name
            if source.exists():
                shutil.copytree(source, contents / name, dirs_exist_ok=True, symlinks=True)
        runtime_report = json.loads((resources / 'ThirdParty/runtime-manifest.json').read_text())
        nested = [contents / path for path in [*runtime_report['bundled_libraries'], *runtime_report.get('runtime_bundles', [])]]
        if any(not path.resolve().is_relative_to(contents.resolve()) or not path.exists() for path in nested):
            raise ValueError('Invalid cached runtime manifest')
    for name in WORKER_RESOURCES:
        shutil.copy2(root / name, resources / name)
    with (resources / 'configuration.json').open('x') as f:
        json.dump({'layout': 'standard-v1', 'pack_previews': '1'}, f)
    shutil.copytree(args.native_encoder, resources / 'TextEncoder', symlinks=False)
    with (contents / 'Info.plist').open('xb') as f:
        plistlib.dump({'CFBundleExecutable': 'Mami', 'CFBundleIdentifier': 'local.mami.prototype',
                       'CFBundleName': 'Mami', 'CFBundleDisplayName': 'Mami', 'CFBundlePackageType': 'APPL',
                       'CFBundleVersion': str(int(datetime.now(timezone.utc).timestamp())),
                       'CFBundleShortVersionString': '0.2.0', 'MamiSourceRevision': args.revision,
                       'LSMinimumSystemVersion': runtime_report['minimum_macos'],
                       'NSHighResolutionCapable': True,
                       # AppKit otherwise swallows main-thread Objective-C exceptions after
                       # unwinding Swift frames, leaving the app running without main-actor work.
                       'NSApplicationCrashOnExceptions': True,
                       'NSPhotoLibraryUsageDescription': PHOTOS_USAGE}, f)
    signing.sign(bundle, nested=nested)
    print(bundle)


if __name__ == '__main__':
    main(parse())
