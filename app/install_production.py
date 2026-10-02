"""Install a verified real app bundle with an atomic, same-volume exchange."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid


RENAME_SWAP,RENAME_EXCL=2,4  # renamex_np flags


def publish_new(source,target):
    """Atomic no-clobber publication, including a destination-created race."""
    library=ctypes.CDLL(None,use_errno=True)
    if library.renamex_np(os.fsencode(source),os.fsencode(target),RENAME_EXCL):
        raise OSError(ctypes.get_errno(),'Cannot publish application',str(target))


def install(bundle,destination,receipt):
    bundle=bundle.resolve();destination=destination.absolute()
    if bundle==destination:raise ValueError('Use a new immutable source bundle')
    subprocess.run(['codesign','--verify','--deep','--strict',str(bundle)],check=True)
    destination.parent.mkdir(parents=True,exist_ok=True)
    temporary=destination.with_name('.Mami-install-'+uuid.uuid4().hex+'.app')
    shutil.copytree(bundle,temporary,symlinks=True)
    subprocess.run(['codesign','--verify','--deep','--strict',str(temporary)],check=True)
    previous=None
    if destination.exists() or destination.is_symlink():
        if destination.name!='Mami.app':raise ValueError('Unexpected application destination')
        previous=str(destination.resolve())
        library=ctypes.CDLL(None,use_errno=True)
        if library.renamex_np(os.fsencode(temporary),os.fsencode(destination),RENAME_SWAP):
            raise OSError(ctypes.get_errno(),'Atomic application exchange failed')
        # Keep the displaced app/link as a recovery artifact; never edit either bundle.
    else:
        publish_new(temporary,destination)
    descriptor=os.open(destination.parent,os.O_RDONLY)
    try:os.fsync(descriptor)
    finally:os.close(descriptor)
    if destination.is_symlink():raise ValueError('Production app must be a real bundle')
    subprocess.run(['codesign','--verify','--deep','--strict',str(destination)],check=True)
    subprocess.run(['/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister','-f',str(destination)],check=True)
    result=dict(status='installed',application=str(destination),source=str(bundle),previous=previous,
                displaced_path=str(temporary) if previous else None,real_bundle=True)
    receipt.parent.mkdir(parents=True,exist_ok=True)
    receipt.write_text(json.dumps(result,indent=2));print(json.dumps(result,indent=2),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle',type=Path,required=True)
    parser.add_argument('--destination',type=Path,default=Path.home()/'Applications/Mami.app')
    parser.add_argument('--receipt',type=Path,required=True)
    args=parser.parse_args();install(args.bundle,args.destination,args.receipt)
