"""Run isolated native release checks sequentially, preserving every log/result."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time


def main(args):
    args.output.mkdir(parents=True,exist_ok=False)
    executable=args.bundle/'Contents/MacOS/Mami'
    base_environment={key:value for key,value in os.environ.items() if not key.startswith('MAMI_')}
    checks=[('scale',args.scale/'catalog',args.scale,args.encoder),
            ('offline',args.offline,args.offline,args.encoder),
            ('visual-failure',args.real/'catalog',args.real,args.output/'deliberately-missing-model')]
    results=[]
    for name,catalog,search,encoder in checks:
        environment=dict(base_environment,MAMI_CATALOG=str(catalog),MAMI_SEARCH_PROJECTION=str(search/'search.sqlite'),
                         MAMI_PACKED_INDEX=str(search/'vectors'),MAMI_NATIVE_ENCODER=str(encoder))
        if name=='visual-failure':environment['MAMI_CHECK_VISUAL_FAILURE']='1'
        started=time.monotonic()
        with (args.output/(name+'.log')).open('x') as log:
            process=subprocess.run([str(executable),'--persistent-search-test',str(args.output/name)],
                                   env=environment,stdout=log,stderr=log,timeout=120)
        if process.returncode:raise RuntimeError(name+' failed; see retained log')
        report=json.loads((args.output/name/'result.json').read_text())
        if name=='offline' and (report['scrub_checks']['original_available'] or not report['scrub_checks']['packed']):
            raise RuntimeError('Offline check did not exercise packed frames with an unavailable original')
        results.append(dict(name=name,wall_seconds=time.monotonic()-started,result=report))
        print(json.dumps(results[-1]),flush=True)
    environment=dict(base_environment,MAMI_CATALOG=str(args.output/'catalog-global'))
    for name,arguments in [('catalog',['--catalog-test',str(args.output/'catalog')]),('worker-pipe',['--worker-pipe-test'])]:
        with (args.output/(name+'.log')).open('x') as log:
            process=subprocess.run([str(executable),*arguments],env=environment,stdout=log,stderr=log,timeout=120)
        if process.returncode:raise RuntimeError(name+' failed; see retained log')
        results.append(dict(name=name,status='passed'))
    if args.fallback_bundle:
        environment=dict(base_environment,MAMI_CATALOG=str(args.offline),MAMI_CHECK_LEGACY_FALLBACK='1')
        with (args.output/'fallback.log').open('x') as log:
            process=subprocess.run([str(args.fallback_bundle/'Contents/MacOS/Mami'),'--persistent-search-test',str(args.output/'fallback')],
                                   env=environment,stdout=log,stderr=log,timeout=180)
        if process.returncode:raise RuntimeError('Crop-aware legacy fallback failed; see retained log')
        results.append(dict(name='fallback',result=json.loads((args.output/'fallback/result.json').read_text())))
    subprocess.run(['codesign','--verify','--strict',str(args.bundle)],check=True)
    (args.output/'result.json').write_text(json.dumps(dict(status='passed',checks=results),indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('bundle','scale','offline','real','encoder','output'):parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--fallback-bundle',type=Path)
    main(parser.parse_args())
