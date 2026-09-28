"""Checksum-verified relocation of this experiment's reproducible large artifacts."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
from common import lock, save


def digest(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source,'sha256').hexdigest()


def entries(root, artifact):
    if artifact not in ('graph-50x.usearch','lance-50x'):
        raise ValueError('Only the explicitly reviewed failed-candidate artifacts may be archived')
    target=root/artifact
    if target.is_symlink():raise ValueError('Refusing a symbolic-link artifact')
    paths=sorted(target.rglob('*')) if target.is_dir() else [target]
    result=[]
    for path in paths:
        if path.is_symlink():raise ValueError('Refusing symbolic links in the artifact')
        if path.is_file():result.append(dict(path=str(path.relative_to(root)),size=path.stat().st_size,sha256=digest(path)))
    if not result:raise ValueError('Artifact is absent or empty')
    return result


def main(args):
    root=args.root.resolve()
    if not root.name.startswith('scaling-') or root.parent.name!='benchmarks':
        raise ValueError('Expected a dedicated scaling experiment directory')
    if args.action=='manifest':
        with lock(root,'archive-'+args.artifact):
            value=dict(source_root=str(root),artifact=args.artifact,files=entries(root,args.artifact))
            save(args.manifest,value)
            print(json.dumps(dict(files=len(value['files']),bytes=sum(v['size'] for v in value['files']),manifest=str(args.manifest))),flush=True)
        return
    value=json.loads(args.manifest.read_text())
    artifact=value['artifact']
    with lock(root,'archive-'+artifact):
        actual=entries(root,artifact)
        if actual!=value['files']:raise ValueError('Artifact bytes or file set differ from manifest')
        receipt=dict(manifest_sha256=digest(args.manifest),verified_root=str(root),files=len(actual),bytes=sum(v['size'] for v in actual))
        if args.action=='verify':
            if not args.receipt:raise ValueError('A receipt path is required')
            save(args.receipt,receipt)
            print(json.dumps(receipt),flush=True)
            return
        if not args.receipt:raise ValueError('Verified destination receipt is required')
        verified=json.loads(args.receipt.read_text())
        if verified['manifest_sha256']!=receipt['manifest_sha256'] or verified['verified_root']==str(root):
            raise ValueError('Need a matching, independently verified destination copy')
        target=root/artifact
        if target.is_dir():shutil.rmtree(target)
        else:target.unlink()
        save(root/('archived-'+artifact+'.json'),dict(artifact=artifact,destination=verified['verified_root'],**receipt))
        print(json.dumps(dict(archived=artifact,destination=verified['verified_root'],bytes=receipt['bytes'])),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['manifest','verify','remove'])
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--artifact',choices=['graph-50x.usearch','lance-50x'])
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--receipt',type=Path)
    main(parser.parse_args())
