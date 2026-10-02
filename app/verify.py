"""Run bounded project checks; terminate the complete test process group on hangs."""
import argparse
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys

SWIFT_CHECK_TIMEOUT_SECONDS = 300
PYTHON_CHECK_TIMEOUT_SECONDS = 120
TERMINATION_GRACE_SECONDS = 5


def check(command, timeout):
    print('CHECK', ' '.join(command), flush=True)
    process = subprocess.Popen(command, cwd=Path(__file__).parent, start_new_session=True)
    try:
        status = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=TERMINATION_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        raise RuntimeError(f'Check exceeded {timeout} seconds: {command[0]}')
    if status:
        raise subprocess.CalledProcessError(status, command)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python-only', action='store_true',
                        help='Linux development checks: Python, plus the platform-independent MamiCore Swift tests when Swift is installed')
    args = parser.parse_args()
    if not args.python_only:
        check(['swift', 'test', '-c', 'release', '--disable-xctest'], SWIFT_CHECK_TIMEOUT_SECONDS)
    elif shutil.which('swift'):
        # The Mami app target needs AppKit, so build only the test product; keep build output out of the tree.
        scratch = str(Path.home() / '.cache' / 'mami-swift-build')
        check(['swift', 'build', '--scratch-path', scratch, '--product', 'MamiPackageTests'], SWIFT_CHECK_TIMEOUT_SECONDS)
        check(['swift', 'test', '--scratch-path', scratch, '--skip-build', '--disable-xctest'], SWIFT_CHECK_TIMEOUT_SECONDS)
    check([sys.executable, '-B', '-m', 'unittest', 'discover', '-s', '.', '-p', 'test_*.py'], PYTHON_CHECK_TIMEOUT_SECONDS)
