"""Explicit user-requested download of pinned optional indexing models."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import urllib.request
import zipfile

from app_paths import models_directory
from model_config import FACE_MODEL, MODELS


def face_model_directory():
    return models_directory() / 'faces' / FACE_MODEL['name']


def face_model_ready(directory=None):
    directory = Path(directory or face_model_directory())
    return all((directory / name).is_file() for name, _ in (FACE_MODEL['detector'], FACE_MODEL['recognizer']))


def install_face_model(archive=None, directory=None):
    """Verify the pinned archive and its two model files, then publish atomically."""
    from face_engine import verify_file
    directory = Path(directory or face_model_directory())
    if face_model_ready(directory):
        for name, digest in (FACE_MODEL['detector'], FACE_MODEL['recognizer']):
            verify_file(directory / name, digest)
        return directory
    directory.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.face-model-', dir=directory.parent) as staging:
        staging = Path(staging)
        if archive is None:
            archive = staging / 'model.zip'
            with urllib.request.urlopen(FACE_MODEL['url'], timeout=60) as response, archive.open('wb') as output:
                shutil.copyfileobj(response, output)
        hasher = hashlib.sha256()
        with Path(archive).open('rb') as handle:
            for block in iter(lambda: handle.read(1 << 20), b''):
                hasher.update(block)
        if hasher.hexdigest() != FACE_MODEL['archive_sha256']:
            raise ValueError('Face model archive checksum mismatch')
        target = staging / FACE_MODEL['name']
        target.mkdir()
        with zipfile.ZipFile(archive) as bundle:
            for name, digest in (FACE_MODEL['detector'], FACE_MODEL['recognizer']):
                member = next(item for item in bundle.namelist() if Path(item).name == name)
                with bundle.open(member) as source, (target / name).open('wb') as output:
                    shutil.copyfileobj(source, output)
                verify_file(target / name, digest)
        os.replace(target, directory)
    return directory


def main():
    os.environ['HF_HUB_OFFLINE']='0'
    os.environ['HF_HUB_DISABLE_IMPLICIT_TOKEN']='1'
    from huggingface_hub import snapshot_download
    for kind,(repository,revision) in MODELS.items():
        print(json.dumps(dict(status='Downloading '+kind+' model')),flush=True)
        snapshot_download(repository,revision=revision,cache_dir=models_directory()/'huggingface/hub',
                          allow_patterns=['*.json','*.safetensors','*.npz','*.bin','*.model','*.txt','*.jinja','LICENSE*','README.md'])
    print(json.dumps(dict(status='Downloading face model')),flush=True)
    install_face_model()
    print(json.dumps(dict(status='Models installed',ready=True)),flush=True)


if __name__=='__main__':main()
