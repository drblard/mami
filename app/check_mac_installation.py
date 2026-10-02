"""Exercise installed storage boundaries and real worker lifetimes on isolated data."""
import argparse
import json
import os
from pathlib import Path
import subprocess


def main(args):
    args.output.mkdir(parents=True,exist_ok=False)
    executable=args.bundle/'Contents/MacOS/Mami'
    base={key:value for key,value in os.environ.items() if not key.startswith(('MAMI_','PYTHON'))}
    reports=[]
    def run(name,flag,environment,timeout=180):
        with (args.output/(name+'.log')).open('x') as log:
            command=[str(executable),flag]
            if flag!='--worker-pipe-test':command.append(str(args.output/name))
            result=subprocess.run(command,env={**base,**environment},stdout=log,stderr=log,timeout=timeout)
        if result.returncode:raise RuntimeError(name+' failed; see retained log')
        reports.append(dict(name=name,status='passed'))
    isolated=dict(MAMI_CATALOG=str(args.output/'catalog-global'))
    run('catalog','--catalog-test',isolated)
    run('transport','--worker-pipe-test',isolated)
    run('media-deletion','--media-deletion-test',isolated)
    fixture=args.output/'worker-support'
    run('workers','--worker-lifecycle-test',dict(MAMI_SUPPORT_ROOT=str(fixture),
        MAMI_MEDIA_ROOT=str(args.output/'originals'),MAMI_MODELS=str(args.support/'Models')),timeout=300)
    personal=fixture/'Personal/user.sqlite';saved=personal.with_suffix('.kept')
    personal.rename(saved)
    try:
        with (args.output/'missing-personal.log').open('x') as log:
            environment={**base,'MAMI_SUPPORT_ROOT':str(fixture),'MAMI_CATALOG':str(fixture/'Derived/Catalog')}
            result=subprocess.run([str(executable),'--persistent-search-test',str(args.output/'missing-personal')],
                                  env=environment,stdout=log,stderr=log,timeout=30)
        text=(args.output/'missing-personal.log').read_text()
        if result.returncode==0 or personal.exists() or 'Personal-data database is missing' not in text:
            raise RuntimeError('Missing personal data was not protected')
        reports.append(dict(name='missing-personal',status='passed'))
    finally:saved.rename(personal)
    subprocess.run(['codesign','--verify','--deep','--strict',str(args.bundle)],check=True)
    report=dict(status='passed',checks=reports)
    (args.output/'result.json').write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('bundle','support','output'):parser.add_argument('--'+name,type=Path,required=True)
    main(parser.parse_args())
