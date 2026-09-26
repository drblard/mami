"""Detached Mac job supervisor. All records are exclusively created, never replaced."""
import json
import os
import select
from pathlib import Path
import subprocess
import sys
import time


def main():
    directory = Path(sys.argv[1])
    spec = json.loads((directory / 'command.json').read_text())
    started = time.time()
    env = dict(os.environ)
    env['PATH'] = '/opt/homebrew/bin:' + env.get('PATH', '/usr/bin:/bin')
    with (directory / 'started.json').open('x') as f:
        json.dump({'pid': os.getpid(), 'started_at': started}, f)
    status = {'exit_code': None}
    try:
        if spec.get('after_pid'):
            queue = select.kqueue()
            try:
                queue.control([select.kevent(spec['after_pid'], filter=select.KQ_FILTER_PROC,
                              flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                              fflags=select.KQ_NOTE_EXIT)], 0, 0)
                queue.control(None, 1, None)
            except ProcessLookupError:
                pass
            finally:
                queue.close()
        with (directory / 'output.log').open('xb') as log:
            child = subprocess.Popen(['/usr/bin/caffeinate', '-is', *spec['argv']],
                                     stdin=subprocess.DEVNULL, stdout=log,
                                     stderr=subprocess.STDOUT, env=env)
            with (directory / 'child.json').open('x') as f:
                json.dump({'pid': child.pid}, f)
            status['exit_code'] = child.wait()
    except Exception as exc:
        status['error'] = str(exc)
    status.update({'finished_at': time.time(), 'elapsed_seconds': time.time() - started})
    with (directory / 'completed.json').open('x') as f:
        json.dump(status, f, indent=2)


if __name__ == '__main__':
    main()
