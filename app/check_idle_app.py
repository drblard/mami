"""Observe the live app reaching quiescence without terminating useful work."""
import argparse
import json
from pathlib import Path
import subprocess
import time


def processes():
    rows=[]
    for line in subprocess.check_output(['ps','-axo','pid,ppid,args'],text=True).splitlines()[1:]:
        values=line.strip().split(None,2)
        if len(values)==3:rows.append((int(values[0]),int(values[1]),values[2]))
    return rows


def main(args):
    subprocess.run(['open','-n',str(args.bundle)],check=True)
    started=time.monotonic();quiet_since=None;observations=[]
    while time.monotonic()-started<180:
        rows=processes()
        app={pid for pid,_,command in rows if command==str(args.bundle/'Contents/MacOS/Mami')}
        descendants=set(app)
        while True:
            expanded=descendants|{pid for pid,parent,_ in rows if parent in descendants}
            if expanded==descendants:break
            descendants=expanded
        helpers=[dict(pid=pid,command=command) for pid,_,command in rows if pid in descendants-app]
        observations.append(dict(seconds=time.monotonic()-started,app_pids=sorted(app),helpers=helpers))
        if app and not helpers:
            if quiet_since is None:quiet_since=time.monotonic()
            if time.monotonic()-quiet_since>=15 and time.monotonic()-started>=30:break
        else:quiet_since=None
        time.sleep(1)
    else:raise TimeoutError('App did not become idle; inspect active work rather than killing it')
    result=dict(status='passed',app_pids=sorted(app),idle_helper_count=0,seconds=time.monotonic()-started,observations=observations)
    args.output.write_text(json.dumps(result,indent=2))
    print(json.dumps({key:value for key,value in result.items() if key!='observations'},indent=2),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    main(parser.parse_args())
