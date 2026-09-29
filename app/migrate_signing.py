"""Preserve the existing identity in developer support storage and macOS Keychain."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess


def run(command):
    result=subprocess.run(command,capture_output=True,text=True)
    if result.returncode:raise RuntimeError('Keychain migration failed: '+result.stderr.strip())
    return result.stdout


def migrate(source,destination,keychain,resume=False):
    source=source.resolve();destination=destination.absolute();keychain=keychain.absolute()
    if (destination.exists() or keychain.exists()) and not resume:
        raise FileExistsError('Migration output exists; inspect it and use --resume without replacing its key')
    identity=json.loads((source/'identity.json').read_text())
    password=Path(identity['passwordFile']).read_text().strip()
    destination.mkdir(parents=True,mode=0o700,exist_ok=resume)
    keychain.parent.mkdir(parents=True,exist_ok=True)
    if not keychain.exists():
        shutil.copy2(identity['keychain'],keychain);keychain.chmod(0o600)
        if hashlib.sha256(Path(identity['keychain']).read_bytes()).digest()!=hashlib.sha256(keychain.read_bytes()).digest():
            raise ValueError('Copied keychain differs')
    run(['/usr/bin/security','unlock-keychain','-p',password,str(keychain)])
    identities=run(['/usr/bin/security','find-identity','-v','-p','codesigning',str(keychain)])
    if identity['fingerprint'] not in identities:raise ValueError('Destination does not contain the original signing identity')
    service='Mami Developer Code Signing';account='Mami'
    run(['/usr/bin/security','add-generic-password','-U','-s',service,'-a',account,'-w',password])
    recovered=run(['/usr/bin/security','find-generic-password','-s',service,'-a',account,'-w']).rstrip('\n')
    if recovered!=password:raise ValueError('Signing password was not preserved in Keychain')
    for name in ('Mami-local-signing.p12','Mami-local-signing.crt'):
        if (destination/name).exists():
            if hashlib.sha256((source/name).read_bytes()).digest()!=hashlib.sha256((destination/name).read_bytes()).digest():
                raise ValueError('Existing recovery file differs; retained without overwrite')
        else:shutil.copy2(source/name,destination/name)
        (destination/name).chmod(0o600)
    updated=dict(fingerprint=identity['fingerprint'],keychain=str(keychain),passwordService=service,passwordAccount=account)
    (destination/'identity.json').write_text(json.dumps(updated,indent=2));(destination/'identity.json').chmod(0o600)
    print(json.dumps(dict(status='migrated',fingerprint=identity['fingerprint'],developer_directory=str(destination),
                          keychain=str(keychain),password_storage='macOS login Keychain',source_retained=True)),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--destination',type=Path,default=Path.home()/'Library/Application Support/Mami Developer/Signing')
    parser.add_argument('--keychain',type=Path,default=Path.home()/'Library/Keychains/Mami-signing.keychain-db')
    parser.add_argument('--resume',action='store_true')
    args=parser.parse_args();migrate(args.source,args.destination,args.keychain,args.resume)
