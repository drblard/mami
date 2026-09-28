"""Publish a verified experiment export as an immutable native encoder artifact."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil

from model_config import VISUAL_MODEL


def prepare(source, destination, float32=False):
    source, destination = Path(source), Path(destination)
    parity = json.loads((source/('coreml-fp32.json' if float32 else 'coreml.json')).read_text())
    tokens = json.loads((source/'tokenizer.json').read_text())
    if parity['cosine_min'] < .9999 or tokens['status'] != 'passed':
        raise ValueError('Source export did not pass the documented parity checks')
    destination.mkdir(parents=True, exist_ok=False)
    shutil.copytree(source/('SiglipTextFP32.mlmodelc' if float32 else 'SiglipText.mlmodelc'), destination/'SiglipText.mlmodelc')
    shutil.copy2(source/'text-encoder/tokenizer.json', destination/'tokenizer.json')
    files = {}
    for path in sorted(destination.rglob('*')):
        if path.is_file():
            with path.open('rb') as stream:
                files[str(path.relative_to(destination))] = hashlib.file_digest(stream,'sha256').hexdigest()
                os.fsync(stream.fileno())
    manifest = dict(model=VISUAL_MODEL,token_count=64,dimensions=768,source=str(source),
                    parity_cosine_min=parity['cosine_min'],files=files,compute_units='cpuOnly')
    with (destination/'manifest.json').open('x') as output:
        json.dump(manifest,output,indent=2);output.flush();os.fsync(output.fileno())
    descriptor=os.open(destination,os.O_RDONLY)
    try:os.fsync(descriptor)
    finally:os.close(descriptor)
    return manifest


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--destination',type=Path,required=True)
    parser.add_argument('--float32',action='store_true')
    args=parser.parse_args()
    print(json.dumps(prepare(args.source,args.destination,args.float32),indent=2))
