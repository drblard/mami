"""Run a build/Keychain command in Terminal's GUI session and await a FIFO event."""
import argparse
import os
from pathlib import Path
import select
import shlex
import subprocess
import sys
import uuid


def main(args):
    args.output.mkdir(parents=True,exist_ok=True)
    name=args.name+'-'+uuid.uuid4().hex[:8]
    fifo=args.output/(name+'.pipe');os.mkfifo(fifo)
    log=args.output/(name+'.log');script=args.output/(name+'.command')
    command=args.command[1:] if args.command[:1]==['--'] else args.command
    if not command:raise ValueError('A command is required')
    script.write_text('#!/bin/bash\n'+shlex.join(command)+' >'+shlex.quote(str(log))+' 2>&1\n'
                      'result=$?\necho "$result" >'+shlex.quote(str(fifo))+'\ncat '+shlex.quote(str(log))+'\nexit "$result"\n')
    script.chmod(0o700)
    descriptor=os.open(fifo,os.O_RDWR|os.O_NONBLOCK)
    try:
        subprocess.run(['open','-g','-a','Terminal',str(script)],check=True)
        if not select.select([descriptor],[],[],args.timeout)[0]:raise TimeoutError('GUI command/approval is still pending: '+str(log))
        result=int(os.read(descriptor,128).decode().strip())
        print(log.read_text(),flush=True)
        return result
    finally:os.close(descriptor);fifo.unlink(missing_ok=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--name',default='command')
    parser.add_argument('--timeout',type=int,default=900)
    parser.add_argument('command',nargs=argparse.REMAINDER)
    sys.exit(main(parser.parse_args()))
