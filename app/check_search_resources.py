"""Measure readiness, latency and process footprints over varied search requests."""
import argparse
import ctypes
from functools import lru_cache
import json
from pathlib import Path
import select
import subprocess
import sys
import time
import threading
import uuid


class ProcessUsage(ctypes.Structure):
    # macOS SDK sys/resource.h, rusage_info_v4. Kernel counters avoid vmmap's
    # intrusive process inspection in the middle of latency measurements.
    _fields_=[('uuid',ctypes.c_uint8*16)]+[(name,ctypes.c_uint64) for name in (
        'user_time','system_time','pkg_idle_wkups','interrupt_wkups','pageins','wired_size','resident_size',
        'phys_footprint','proc_start_abstime','proc_exit_abstime','child_user_time','child_system_time',
        'child_pkg_idle_wkups','child_interrupt_wkups','child_pageins','child_elapsed_abstime',
        'diskio_bytesread','diskio_byteswritten','cpu_time_qos_default','cpu_time_qos_maintenance',
        'cpu_time_qos_background','cpu_time_qos_utility','cpu_time_qos_legacy','cpu_time_qos_user_initiated',
        'cpu_time_qos_user_interactive','billed_system_time','serviced_system_time','logical_writes',
        'lifetime_max_phys_footprint','instructions','cycles','billed_energy','serviced_energy',
        'interval_max_phys_footprint','runnable_time')]


@lru_cache(maxsize=1)
def usage_function():
    function=ctypes.CDLL('/usr/lib/libproc.dylib',use_errno=True).proc_pid_rusage
    function.argtypes=[ctypes.c_int,ctypes.c_int,ctypes.c_void_p]
    function.restype=ctypes.c_int
    return function


def usage(pid):
    value=ProcessUsage()
    if usage_function()(pid,4,ctypes.byref(value)):
        raise OSError(ctypes.get_errno(),'Cannot read process resource counters',pid)
    return dict(footprint_MiB=value.phys_footprint/1024**2,
                peak_MiB=value.lifetime_max_phys_footprint/1024**2)


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
        result.append(dict(pid=pid,command=command,**usage(pid)))
    return result


def main(args):
    args.output.mkdir(parents=True,exist_ok=False)
    sys.path.insert(0,str(args.bundle/'Contents/Resources'))
    from vector_sync import publish
    old_pointer=None
    replacement=None
    if args.swap:
        if 'benchmarks' not in args.vectors.resolve().parts:
            raise ValueError('Generation-swap experiments require an isolated benchmark vector root')
        old_pointer=json.loads((args.vectors/'current.json').read_text())
        replacement=args.vectors/('generation-resource-'+uuid.uuid4().hex)
        subprocess.run(['cp','-cR',str(args.vectors/old_pointer['generation']),str(replacement)],check=True)
    command=[sys.executable,'-B',str(args.bundle/'Contents/Resources/search_worker.py'),
             '--index',str(args.index),'--packed-index',str(args.vectors),'--projection',str(args.projection),
             '--native-encoder',str(args.encoder)]
    with (args.output/'worker.log').open('x') as errors:
        started=time.monotonic()
        process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=errors,text=True)
        stop=threading.Event()
        observations=[]
        monitor=None
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
            pids=[row['pid'] for row in before]
            def observe():
                while not stop.wait(.1):
                    sample=[usage(pid)['footprint_MiB'] for pid in pids]
                    observations.append(dict(seconds=time.monotonic()-started,total_MiB=sum(sample)))
            monitor=threading.Thread(target=observe);monitor.start()
            timings=[]
            swap_confirmed=False
            swap_results=None
            phrases=['milking goats','children playing','picking plums','a river','a family eating','walking outside']
            for i in range(args.queries):
                if replacement and i==args.queries//2:
                    swap_results={hit['path'] for hit in request('milking goats','visual')['hits']}
                    publish(args.vectors,replacement)
                tick=time.monotonic();reply=request(f'{phrases[i%len(phrases)]} {i}','both')
                timings.append((time.monotonic()-tick)*1000)
                if reply.get('visual_pending'):raise RuntimeError('Ready visual service regressed to pending')
            if replacement:
                deadline=time.monotonic()+20
                while time.monotonic()<deadline:
                    mappings=subprocess.run(['lsof','-Fn','-p',','.join(map(str,pids))],capture_output=True,text=True)
                    if str(replacement/'rows.sqlite') in mappings.stdout:
                        swap_confirmed=True;break
                    time.sleep(.1)
                if not swap_confirmed:raise RuntimeError('Visual process did not acquire the replacement generation')
                if {hit['path'] for hit in request('milking goats','visual')['hits']}!=swap_results:
                    raise RuntimeError('Generation swap changed identical-generation results')
            after=footprints(process.pid)
            stop.set();monitor.join(timeout=35)
            timings.sort()
            result=dict(ready=ready,text_ready_seconds=text_ready,visual_ready_seconds=visual_ready,first_text_ms=first_text_ms,
                        query_count=len(timings),median_ms=timings[len(timings)//2],p95_ms=timings[int(len(timings)*.95)],max_ms=max(timings),
                        footprints_before=before,footprints_after=after,
                        memory_observations=observations,hot_generation_swap=swap_confirmed,
                        caveat='Fresh processes, potentially warm OS caches. Varied queries exercise bounded encoder cache; memory uses nonintrusive kernel rusage counters. Synthetic corpus quality is checked separately.')
            (args.output/'result.json').write_text(json.dumps(result,indent=2))
            print(json.dumps({key:value for key,value in result.items() if key!='memory_observations'},indent=2),flush=True)
        finally:
            stop.set()
            if monitor:monitor.join(timeout=35)
            process.stdin.close()
            try:process.wait(timeout=20)
            except subprocess.TimeoutExpired:process.kill();process.wait()
            if old_pointer is not None:
                publish(args.vectors,args.vectors/old_pointer['generation'])


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('bundle','index','vectors','projection','encoder','output'):parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--queries',type=int,default=256)
    parser.add_argument('--swap',action='store_true')
    main(parser.parse_args())
