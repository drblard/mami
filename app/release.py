"""Build, test, package and (with --install) install Mami on the Mac, from a clean commit.

Every release is a new immutable snapshot under ~/mami-lab/apps; a live signed
bundle is never modified. Installing quits Mami normally (never forcibly), swaps the
bundle atomically, relaunches it, and verifies the installed revision and that
personal data is unchanged. The displaced bundle is kept for rollback.
"""
import argparse
import json
import shlex
import subprocess
import sys
import time

from deploy import HOST, ROOT, deploy, revision
from personal_audit import changed_tables

PYTHON = '/Users/ludi/mami-lab/.venv/bin/python'  # Build-time Python with test dependencies.
APPLICATION = '/Users/ludi/Applications/Mami.app'
SUPPORT = '/Users/ludi/Library/Application Support/Mami'
EDITOR_BUNDLE = 'com.lemon.lvoverseas'  # CapCut; matches Indexing.activeEditing.
ACTIVE_INPUT_SECONDS = 120  # Input more recent than this means someone is using the Mac.
QUIT_TIMEOUT_SECONDS = 60
LAUNCH_TIMEOUT_SECONDS = 60
STEP_TIMEOUT_SECONDS = 1800


def remote(command, timeout=STEP_TIMEOUT_SECONDS, check=True):
    result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', HOST, command],
                            capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        sys.stderr.write(result.stdout[-4000:] + result.stderr[-4000:])
        raise SystemExit(f'Failed on {HOST}: {command[:120]}')
    return result.stdout.strip()


def step(message):
    print(f'== {message}', flush=True)


def activity():
    """(frontmost bundle identifier, seconds since the last input)."""
    output = remote('osascript -e \'tell application "System Events" to get bundle identifier of first application process whose frontmost is true\'; '
                    'ioreg -c IOHIDSystem | awk \'/HIDIdleTime/ {print int($NF/1000000000); exit}\'')
    frontmost, idle = output.splitlines()
    return frontmost, int(idle)


def require_quiet(installing):
    frontmost, idle = activity()
    if frontmost == EDITOR_BUNDLE and idle < ACTIVE_INPUT_SECONDS:
        raise SystemExit('CapCut is being used on the Mac; try again when editing has paused.')
    if installing and idle < ACTIVE_INPUT_SECONDS:
        raise SystemExit(f'Someone used the Mac {idle}s ago ({frontmost}); installing restarts Mami. Use --now to proceed anyway.')


def running(pattern):
    return remote(f'pgrep -f {shlex.quote(pattern)}', check=False) != ''


def wait_until(condition, seconds, message):
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > deadline:
            raise SystemExit(message)
        time.sleep(1)


def audit(snapshot):
    database = shlex.quote(f'{SUPPORT}/Personal/user.sqlite')
    return json.loads(remote(f'cd {snapshot} && {PYTHON} personal_audit.py --database {database}'))


def install(snapshot, source_revision):
    if running('import_media.py|photos_batch.py'):
        raise SystemExit('An import is running; install after it finishes.')
    step('Quitting Mami')
    if running('Mami.app/Contents/MacOS/Mami$'):  # Telling a stopped app to quit can launch it.
        remote('osascript -e \'tell application "Mami" to quit\'', check=False)
    wait_until(lambda: not running('Mami.app/Contents/(MacOS/Mami$|Helpers)'), QUIT_TIMEOUT_SECONDS,
               'Mami did not quit; it was not forced. Quit it on the Mac and retry.')
    before = audit(snapshot)
    step('Installing')
    receipt = json.loads(remote(f'cd {snapshot} && {PYTHON} install_production.py --bundle {snapshot}/Mami.app --receipt {snapshot}/install-receipt.json'))
    remote(f'open {APPLICATION}')
    wait_until(lambda: running('Mami.app/Contents/MacOS/Mami$'), LAUNCH_TIMEOUT_SECONDS, 'Installed Mami did not start.')
    installed = remote(f'plutil -extract MamiSourceRevision raw {APPLICATION}/Contents/Info.plist')
    if installed != source_revision:
        raise SystemExit(f'Installed revision {installed} differs from {source_revision}')
    changed = changed_tables(before, audit(snapshot))
    if changed:
        raise SystemExit(f'Personal tables changed during install: {changed}. Previous bundle: {receipt["displaced_path"]}')
    return receipt


def main(args):
    source_revision = revision()
    if source_revision.endswith('-dirty') and not args.allow_dirty:
        raise SystemExit('Commit or stash changes first; releases are built from a commit (--allow-dirty for a test build).')
    if not args.skip_local_checks:
        step('Linux checks')
        subprocess.run([sys.executable, 'verify.py', '--python-only'], cwd=ROOT, check=True)
    require_quiet(installing=False)
    step(f'Deploying {source_revision}')
    snapshot = deploy(source_revision)
    print(snapshot, flush=True)
    step('Building and testing on the Mac')
    remote(f'cd {snapshot} && caffeinate -is swift build -c release && caffeinate -is {PYTHON} verify.py')
    step('Packaging and signing (GUI session holds the signing keychain)')
    runtime = shlex.quote(f'{args.runtime_from}/Contents')
    encoder = shlex.quote(f'{args.runtime_from}/Contents/Resources/TextEncoder')
    remote(f'cd {snapshot} && {PYTHON} run_gui_command.py --output {snapshot}/gui --name package -- '
           f'{PYTHON} {snapshot}/package.py --runtime-tree {runtime} --native-encoder {encoder} --revision {source_revision}')
    remote(f'codesign --verify --deep --strict {snapshot}/Mami.app')
    step('Installation checks on isolated data')
    remote(f'cd {snapshot} && {PYTHON} check_mac_installation.py --bundle {snapshot}/Mami.app '
           f'--support {shlex.quote(SUPPORT)} --output {snapshot}/installation-check')
    record = dict(snapshot=snapshot, revision=source_revision, installed=False)
    if args.install:
        if not args.now:
            require_quiet(installing=True)
        receipt = install(snapshot, source_revision)
        record.update(installed=True, previous=receipt['displaced_path'])
    print(json.dumps(record, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install', action='store_true', help='Install and relaunch after all checks pass')
    parser.add_argument('--now', action='store_true', help='Install even if the Mac was used in the last two minutes')
    parser.add_argument('--allow-dirty', action='store_true', help='Build uncommitted changes (never for an install)')
    parser.add_argument('--skip-local-checks', action='store_true')
    parser.add_argument('--runtime-from', default=APPLICATION, help='Verified bundle whose Python/media runtime and text encoder are reused')
    args = parser.parse_args()
    if args.install and args.allow_dirty:
        parser.error('Install only committed revisions')
    main(args)
