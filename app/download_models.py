"""Explicit user-requested download of pinned optional indexing models."""
import json
import os

from app_paths import models_directory
from model_config import MODELS


def main():
    os.environ['HF_HUB_OFFLINE']='0'
    os.environ['HF_HUB_DISABLE_IMPLICIT_TOKEN']='1'
    from huggingface_hub import snapshot_download
    for kind,(repository,revision) in MODELS.items():
        print(json.dumps(dict(status='Downloading '+kind+' model')),flush=True)
        snapshot_download(repository,revision=revision,cache_dir=models_directory()/'huggingface/hub',
                          allow_patterns=['*.json','*.safetensors','*.npz','*.bin','*.model','*.txt','*.jinja','LICENSE*','README.md'])
    print(json.dumps(dict(status='Models installed',ready=True)),flush=True)


if __name__=='__main__':main()
