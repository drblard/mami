"""Deploy an immutable source snapshot under ~/mami-lab/apps on the Mac."""
import io
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path

root = Path(__file__).resolve().parent
name = 'prototype-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
remote = '/Users/ludi/mami-lab/apps/' + name
archive = io.BytesIO()
with tarfile.open(fileobj=archive, mode='w') as tar:
    for relative in ['Package.swift', 'Sources', 'search_worker.py', 'package.py', 'metadata.py', 'build_catalog.py', 'restore_catalog.py', 'relocate_catalog.py', 'index_worker.py', 'index_queue.py', 'index_backend.py', 'import_media.py', 'gpu_activity.py', 'photos_batch.py', 'test_photos_batch.py', 'test_import_media.py', 'test_index_queue.py']:
        tar.add(root / relative, arcname=relative)
    tar.add(root.parent / 'feasibility/lab.py', arcname='lab.py')
subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', 'mami-mac',
                f'mkdir -p /Users/ludi/mami-lab/apps && mkdir {remote} && tar -x -C {remote}'],
               input=archive.getvalue(), check=True, timeout=30)
print(remote)
