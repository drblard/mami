"""Deploy an immutable source snapshot under ~/mami-lab/apps on the Mac."""
import io
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from bundle_resources import RELEASE_TOOLS, WORKER_RESOURCES

ROOT = Path(__file__).resolve().parent
HOST = 'ludi'
APPS = '/Users/ludi/mami-lab/apps'


def revision():
    """The source revision; `-dirty` marks uncommitted changes."""
    return subprocess.run(['git', 'describe', '--always', '--dirty', '--abbrev=12'], cwd=ROOT,
                          capture_output=True, text=True, check=True).stdout.strip()


def deploy(source_revision):
    remote = f'{APPS}/prototype-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode='w') as tar:
        for relative in [*RELEASE_TOOLS, *WORKER_RESOURCES, *(path.name for path in sorted(ROOT.glob('test_*.py')))]:
            tar.add(ROOT / relative, arcname=relative)
        stamp = (source_revision + '\n').encode()
        info = tarfile.TarInfo('REVISION')
        info.size = len(stamp)
        tar.addfile(info, io.BytesIO(stamp))
    subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', HOST,
                    f'mkdir -p {APPS} && mkdir {remote} && tar -x -C {remote}'],
                   input=archive.getvalue(), check=True, timeout=30)
    return remote


if __name__ == '__main__':
    print(deploy(revision()))
