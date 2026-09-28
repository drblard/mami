"""Production model pins and cache policy; independent of experiment tooling."""
import os
from pathlib import Path

VISUAL_MODEL = ('google/siglip2-base-patch16-224', '75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2')
SPEECH_MODEL = ('mlx-community/whisper-large-v3-turbo', 'a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb')
MODELS = {'visual': VISUAL_MODEL, 'speech': SPEECH_MODEL}

VISUAL_CPU_THREADS = 4
SPEECH_CACHE_BYTES = 256 * 1024 * 1024
FRAME_INTERVAL_SECONDS = 1
SPEECH_CHUNK_SECONDS = 30
SPEECH_LANGUAGE = 'ro'
PIPELINE_VERSION = 1
PIPELINE = (f'siglip2-{VISUAL_MODEL[1][:8]}-whisper-{SPEECH_MODEL[1][:8]}-'
            f'{FRAME_INTERVAL_SECONDS}s-{SPEECH_LANGUAGE}-chunk{SPEECH_CHUNK_SECONDS}-v{PIPELINE_VERSION}')


def lab_directory():
    return Path(os.environ.get('MAMI_LAB', Path.home() / 'mami-lab'))


def configure_cache_environment():
    cache = lab_directory() / 'cache'
    os.environ['HF_HOME'] = str(cache / 'huggingface')
    os.environ['TORCH_HOME'] = str(cache / 'torch')
    os.environ['XDG_CACHE_HOME'] = str(cache)
    os.environ['TOKENIZERS_PARALLELISM'] = 'false'
    os.environ['HF_HUB_OFFLINE'] = '1'


def cached_model_path(kind):
    from huggingface_hub import snapshot_download
    repository, revision = MODELS[kind]
    return snapshot_download(repository, revision=revision, local_files_only=True,
                             cache_dir=lab_directory() / 'cache/huggingface/hub',
                             allow_patterns=['*.json', '*.safetensors', '*.npz', '*.bin', '*.model', '*.txt', '*.jinja'])
