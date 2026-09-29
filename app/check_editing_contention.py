"""Idle-window check: CapCut present, controlled decode/import/AI load, live search."""
import argparse
import contextlib
import json
from pathlib import Path
import select
import sqlite3
import subprocess
import sys
import time


def main(args):
    import numpy as np
    args.output.mkdir(parents=True,exist_ok=False)
    resources=args.bundle/'Contents/Resources'
    sys.path.insert(0,str(resources))
    from index_store import connection
    subprocess.run(['open','-a','CapCut'],check=True)
    clip=max(args.source.glob('*.MP4'),key=lambda path:path.stat().st_size)
    workers=[];timings=[]
    with contextlib.ExitStack() as stack:
        def start(command,name,pipe=False):
            log=stack.enter_context((args.output/(name+'.log')).open('x'))
            process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE if pipe else log,stderr=log)
            workers.append(process)
            return process
        search=start([sys.executable,'-B',str(resources/'search_worker.py'),'--index',str(args.index),
                      '--packed-index',str(args.scale/'vectors'),'--projection',str(args.scale/'search.sqlite'),
                      '--native-encoder',str(args.encoder)],'search',True)
        def read():
            if not select.select([search.stdout],[],[],30)[0]:raise TimeoutError('Contended search deadline')
            reply=json.loads(search.stdout.readline())
            if reply.get('error'):raise RuntimeError(reply['error'])
            return reply
        def query(text,mode='both'):
            started=time.monotonic()
            search.stdin.write((json.dumps(dict(query=text,mode=mode))+'\n').encode());search.stdin.flush()
            reply=read()
            return reply,(time.monotonic()-started)*1000
        try:
            read()
            deadline=time.monotonic()+30
            while query('a photo')[0].get('visual_pending'):
                if time.monotonic()>deadline:raise TimeoutError('Visual warmup deadline')
                time.sleep(.05)
            decoder=start(['/opt/homebrew/bin/ffmpeg','-nostdin','-v','error','-re','-stream_loop','-1',
                           '-hwaccel','videotoolbox','-i',str(clip),'-vf','scale=1920:-2','-threads','2','-f','null','-'],'decode')
            imported=args.output/'import'
            importer=start([sys.executable,'-B',str(args.bundle.parent/'check_dji_responsiveness.py'),'--bundle',str(args.bundle),
                            '--source',str(args.source),'--output',str(imported),'--timeout','120'],'import')
            phrases=['milking goats','bringing food to goats','dunare','children playing','picking plums','a family eating together']
            count=0;deadline=time.monotonic()+150
            while importer.poll() is None or count<128:
                if time.monotonic()>deadline:raise TimeoutError('Contended import deadline')
                reply,elapsed=query(phrases[count%len(phrases)],'speech' if count%4==0 else 'both')
                if reply.get('visual_pending'):raise RuntimeError('Visual service lost readiness')
                timings.append(dict(phase='import-preview',mode='speech' if count%4==0 else 'both',ms=elapsed));count+=1
            if importer.returncode:raise RuntimeError('Contended import failed; see retained log')
            if decoder.poll() is not None:raise RuntimeError('Controlled decoder exited unexpectedly')
            database=imported/'catalog.sqlite'
            with connection(database) as db:db.execute('UPDATE scan_control SET paused=0')
            indexer=start([sys.executable,'-B',str(resources/'index_worker.py'),'--database',str(database),'--root',str(imported/'originals'),
                           '--artifacts',str(imported/'artifacts'),'--role','index','--once'],'index')
            deadline=time.monotonic()+300;count=0
            while indexer.poll() is None:
                if time.monotonic()>deadline:raise TimeoutError('Controlled indexing deadline')
                _,elapsed=query(phrases[count%len(phrases)],'speech' if count%4==0 else 'both')
                timings.append(dict(phase='index-transcription',mode='speech' if count%4==0 else 'both',ms=elapsed));count+=1
            if indexer.returncode:raise RuntimeError('Controlled indexing failed')
            with contextlib.closing(sqlite3.connect(database)) as db:
                units=dict(db.execute('SELECT stage,count(*) FROM index_units GROUP BY stage').fetchall())
                jobs=dict(db.execute('SELECT state,count(*) FROM index_jobs GROUP BY state').fetchall())
            if not units.get('embedding') or not units.get('speech') or set(jobs)!={'complete'}:
                raise RuntimeError('Controlled workload did not complete both visual and speech indexing')
            capcut=subprocess.run(['pgrep','-x','CapCut'],capture_output=True,text=True)
            if capcut.returncode:raise RuntimeError('CapCut was not present during the check')
            summaries=[]
            for phase in ('import-preview','index-transcription'):
                for mode in ('speech','both'):
                    values=[row['ms'] for row in timings if row['phase']==phase and row['mode']==mode]
                    summaries.append(dict(phase=phase,mode=mode,queries=len(values),p95_ms=float(np.percentile(values,95)),max_ms=max(values)))
            result=dict(status='passed',capcut_pids=capcut.stdout.split(),summaries=summaries,units=units,jobs=jobs,
                        import_result=json.loads((imported/'result.json').read_text()),
                        caveat='CapCut application present; controlled real-DJI hardware decode/scale, verified import, previews and actual inference. No editing gestures or changes to the user’s CapCut projects.')
            (args.output/'result.json').write_text(json.dumps(result,indent=2));print(json.dumps(result,indent=2),flush=True)
        finally:
            for process in reversed(workers):
                if process.poll() is None:
                    process.stdin.close();process.terminate()
                    try:process.wait(timeout=10)
                    except subprocess.TimeoutExpired:process.kill();process.wait()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('bundle','scale','source','index','encoder','output'):parser.add_argument('--'+name,type=Path,required=True)
    main(parser.parse_args())
