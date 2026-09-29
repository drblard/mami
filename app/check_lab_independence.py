"""Temporarily hide the development tree and validate the real installed app."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time
import uuid


def main(args):
    args.output.mkdir(parents=True,exist_ok=False)
    if args.bundle.is_symlink():raise ValueError('Installed app is still a symlink')
    bundle=args.bundle.resolve()
    if bundle.is_relative_to(args.lab):raise ValueError('App is still inside the lab')
    hidden=args.lab.with_name(args.lab.name+'.unavailable-'+uuid.uuid4().hex[:8])
    environment={key:value for key,value in os.environ.items() if not key.startswith(('MAMI_','PYTHON'))}
    environment.update(MAMI_CATALOG=str(args.support/'Derived/Catalog'),PATH='/usr/bin:/bin:/usr/sbin:/sbin')
    args.lab.rename(hidden)
    try:
        started=time.monotonic()
        with (args.output/'native.log').open('x') as log:
            subprocess.run([str(bundle/'Contents/MacOS/Mami'),'--persistent-search-test',str(args.output/'native')],
                           env=environment,stdout=log,stderr=log,check=True,timeout=120)
        python=bundle/'Contents/Helpers/MamiPython'
        probe='import sys,json,numpy,torch,mlx.core; from PIL import Image; print(json.dumps(dict(executable=sys.executable,prefix=sys.prefix,paths=sys.path)))'
        runtime=subprocess.check_output([str(python),'-B','-c',probe],env=environment,text=True,timeout=45,cwd=args.output)
        runtime_info=json.loads(runtime)
        if any(str(args.lab) in value for value in [runtime_info['executable'],runtime_info['prefix'],*runtime_info['paths']]):
            raise ValueError('Runtime still resolves a lab path')
        subprocess.run([str(bundle/'Contents/Helpers/ffmpeg'),'-v','error','-f','lavfi','-i','color=c=blue:s=32x32:r=1','-t','1','-f','null','-'],
                       env=environment,check=True,timeout=20)
        audio=args.output/'silence.wav'
        subprocess.run([str(bundle/'Contents/Helpers/ffmpeg'),'-v','error','-n','-f','lavfi','-i','anullsrc=r=16000:cl=mono','-t','1',str(audio)],
                       env=environment,check=True,timeout=20)
        speech_code='import sys,json; from pathlib import Path; sys.path.insert(0,sys.argv[1]); from index_backend import Backend; b=Backend(Path(sys.argv[2])); print(json.dumps(dict(speech_segments=len(b.speech(Path(sys.argv[3]),0)))))'
        with (args.output/'speech.log').open('x') as log:
            subprocess.run([str(python),'-B','-c',speech_code,str(bundle/'Contents/Resources'),str(args.output),str(audio)],
                           env=environment,stdout=log,stderr=log,check=True,timeout=120,cwd=args.output)
        subprocess.run(['codesign','--verify','--deep','--strict',str(bundle)],check=True)
        result=dict(status='passed',lab_unavailable=True,seconds=time.monotonic()-started,runtime=runtime_info,
                    native=json.loads((args.output/'native/result.json').read_text()))
        (args.output/'result.json').write_text(json.dumps(result,indent=2));print(json.dumps(result,indent=2),flush=True)
    finally:
        if args.lab.exists():raise RuntimeError('Another process recreated the lab path; preserved both trees for review')
        hidden.rename(args.lab)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('bundle','lab','support','output'):parser.add_argument('--'+name,type=Path,required=True)
    main(parser.parse_args())
