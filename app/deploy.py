"""Deploy an immutable source snapshot under ~/mami-lab/apps on the Mac."""
import io
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from bundle_resources import WORKER_RESOURCES

root = Path(__file__).resolve().parent
name = 'prototype-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
remote = '/Users/ludi/mami-lab/apps/' + name
archive = io.BytesIO()
with tarfile.open(fileobj=archive, mode='w') as tar:
    deployment_files = ['Package.swift', 'Sources', 'Tests', 'benchmark_search.py', 'package.py',
                        'bundle_resources.py', 'signing.py', 'build_catalog.py', 'restore_catalog.py',
                        'relocate_catalog.py', 'prune_catalog_backups.py', 'check_media_pipeline.py', 'verify.py', 'build_search_store.py', 'prepare_text_encoder.py']
    deployment_files += list(WORKER_RESOURCES)
    if (root / 'Package.resolved').exists():
        deployment_files.append('Package.resolved')
    deployment_files += [path.name for path in sorted(root.glob('test_*.py'))]
    for relative in deployment_files:
        tar.add(root / relative, arcname=relative)
subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', 'ludi',
                f'mkdir -p /Users/ludi/mami-lab/apps && mkdir {remote} && tar -x -C {remote}'],
               input=archive.getvalue(), check=True, timeout=30)
print(remote)
