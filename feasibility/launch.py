"""Launch a detached, logged Mac job; command arguments use absolute Mac paths."""
import argparse
import base64
import json
from pathlib import Path
import shlex
import subprocess

parser = argparse.ArgumentParser()
parser.add_argument('--label', required=True)
parser.add_argument('--after-pid', type=int, help='Wait for this existing Mac process to exit first')
parser.add_argument('argv', nargs=argparse.REMAINDER)
args = parser.parse_args()
argv = args.argv[1:] if args.argv[:1] == ['--'] else args.argv
if not argv:
    parser.error('provide a command after --')
payload = {'label': args.label, 'argv': argv, 'after_pid': args.after_pid,
           'runner': base64.b64encode(Path(__file__).with_name('job_runner.py').read_bytes()).decode()}
bootstrap = '''
import base64, datetime, json, pathlib, re, subprocess, sys
spec = json.load(sys.stdin)
label = re.sub('[^a-zA-Z0-9_-]', '-', spec['label'])[:60]
stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
directory = pathlib.Path.home() / 'mami-lab/jobs' / (label + '-' + stamp)
directory.mkdir(parents=True, exist_ok=False)
runner = directory / 'runner.py'
with runner.open('xb') as f:
    f.write(base64.b64decode(spec.pop('runner')))
with (directory / 'command.json').open('x') as f:
    json.dump(spec, f, indent=2)
with (directory / 'supervisor.log').open('xb') as log:
    child = subprocess.Popen([sys.executable, str(runner), str(directory)],
        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True, close_fds=True)
print(json.dumps({'job_directory': str(directory), 'supervisor_pid': child.pid}))
'''
subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', 'mami-mac',
                '"$HOME/mami-lab/.venv/bin/python" -c ' + shlex.quote(bootstrap)],
               input=json.dumps(payload).encode(), check=True)
