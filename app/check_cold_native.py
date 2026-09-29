"""Run an isolated native acceptance check immediately after authorized macOS purge."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time
import threading


def main(args):
    args.output.mkdir(parents=True,exist_ok=False)
    subprocess.run(['osascript','-e','do shell script "/usr/sbin/purge" with administrator privileges'],check=True)
    base_environment={key:value for key,value in os.environ.items() if not key.startswith('MAMI_')}
    environment=dict(base_environment,MAMI_CATALOG=str(args.fixture/'catalog'),
                     MAMI_SEARCH_PROJECTION=str(args.fixture/'search.sqlite'),
                     MAMI_PACKED_INDEX=str(args.fixture/'vectors'),MAMI_NATIVE_ENCODER=str(args.encoder))
    started=time.monotonic()
    milestones={}
    with (args.output/'native.log').open('x') as log:
        process=subprocess.Popen([str(args.bundle/'Contents/MacOS/Mami'),'--persistent-search-test',str(args.output/'native')],
                                 env=environment,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
        def capture():
            for line in process.stdout:
                if line.startswith('MAMI_READINESS:'):
                    milestones[line.strip().split(':',1)[1]]=time.monotonic()-started
                log.write(line);log.flush()
        reader=threading.Thread(target=capture);reader.start()
        try:status=process.wait(timeout=120)
        except subprocess.TimeoutExpired:
            process.kill();process.wait();raise
        finally:reader.join(timeout=10)
    elapsed=time.monotonic()-started
    native_path=args.output/'native/result.json'
    report=dict(method='Successful macOS disk-buffer purge followed immediately by a fresh native process; not a reboot.',
                process_wall_seconds=elapsed,launch_to_readiness_seconds=milestones,exit_status=status,
                native=json.loads(native_path.read_text()) if native_path.exists() else None)
    (args.output/'result.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2),flush=True)
    if status:raise RuntimeError('Cold native check failed; see native.log')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('bundle','fixture','encoder','output'):parser.add_argument('--'+name,type=Path,required=True)
    main(parser.parse_args())
