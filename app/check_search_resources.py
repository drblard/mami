"""Measure readiness, latency and process footprints over varied search requests."""
import argparse
import json
from pathlib import Path
import re
import select
import subprocess
import sys
import time


def footprints(root_pid):
    processes=[]
    for line in subprocess.check_output(['ps','-axo','pid,ppid,comm'],text=True).splitlines()[1:]:
        parts=line.strip().split(None,2)
        if len(parts)==3:processes.append((int(parts[0]),int(parts[1]),parts[2]))
    pids={root_pid}
    while True:
        expanded=pids|{pid for pid,parent,_ in processes if parent in pids}
        if expanded==pids:break
        pids=expanded
    result=[]
    for pid,_,command in processes:
        if pid not in pids:continue
        report=subprocess.run(['vmmap','-summary',str(pid)],capture_output=True,text=True,timeout=30)
        match=re.search(r'Physical footprint:\s+([\d.]+)([KMG])',report.stdout)
        result.append(dict(pid=pid,command=command,footprint_MiB=float(match[1])*{'K':1/1024,'M':1,'G':1024}[match[2]] if match else None))
    return result


def main(args):
    args.output.mkdir(parents=True,exist_ok=False)
    command=[sys.executable,'-B',str(args.bundle/'Contents/Resources/search_worker.py'),
             '--index',str(args.index),'--packed-index',str(args.vectors),'--projection',str(args.projection),
             '--native-encoder',str(args.encoder)]
    with (args.output/'worker.log').open('x') as errors:
        started=time.monotonic()
        process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=errors,text=True)
        def read():
            if not select.select([process.stdout],[],[],30)[0]:raise TimeoutError('Search response deadline')
            result=json.loads(process.stdout.readline())
            if 'error' in result:raise RuntimeError(result['error'])
            return result
        def request(query,mode):
            process.stdin.write(json.dumps(dict(query=query,mode=mode))+'\n');process.stdin.flush()
            return read()
        try:
            ready=read();text_ready=time.monotonic()-started
            first_text_started=time.monotonic();request('dunare','speech');first_text_ms=(time.monotonic()-first_text_started)*1000
            deadline=time.monotonic()+30
            while request('a photo','visual').get('visual_pending'):
                if time.monotonic()>deadline:raise TimeoutError('Visual initialization deadline')
                time.sleep(.05)
            visual_ready=time.monotonic()-started
            before=footprints(process.pid)
            timings=[]
            phrases=['milking goats','children playing','picking plums','a river','a family eating','walking outside']
            for i in range(args.queries):
                tick=time.monotonic();reply=request(f'{phrases[i%len(phrases)]} {i}','both')
                timings.append((time.monotonic()-tick)*1000)
                if reply.get('visual_pending'):raise RuntimeError('Ready visual service regressed to pending')
            after=footprints(process.pid)
            timings.sort()
            result=dict(ready=ready,text_ready_seconds=text_ready,visual_ready_seconds=visual_ready,first_text_ms=first_text_ms,
                        query_count=len(timings),median_ms=timings[len(timings)//2],p95_ms=timings[int(len(timings)*.95)],max_ms=max(timings),
                        footprints_before=before,footprints_after=after,
                        caveat='Fresh processes, potentially warm OS caches. Varied queries exercise bounded encoder cache; synthetic corpus quality is checked separately.')
            (args.output/'result.json').write_text(json.dumps(result,indent=2));print(json.dumps(result,indent=2),flush=True)
        finally:
            process.stdin.close()
            try:process.wait(timeout=20)
            except subprocess.TimeoutExpired:process.kill();process.wait()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('bundle','index','vectors','projection','encoder','output'):parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--queries',type=int,default=256)
    main(parser.parse_args())
