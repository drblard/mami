"""Production model pins and cache policy; independent of experiment tooling."""
import os
from app_paths import models_directory,cache_directory

VISUAL_MODEL = ('google/siglip2-base-patch16-224', '75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2')
SPEECH_MODEL = ('mlx-community/whisper-large-v3-turbo', 'a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb')
MODELS = {'visual': VISUAL_MODEL, 'speech': SPEECH_MODEL}

# InsightFace antelopev2 (SCRFD-10G detector + ArcFace ResNet-100 on Glint360K).
# Weights are licensed for non-commercial use only; this is a private family app.
FACE_MODEL = dict(
    name='antelopev2',
    url='https://github.com/deepinsight/insightface/releases/download/v0.7/antelopev2.zip',
    archive_sha256='8e182f14fc6e80b3bfa375b33eb6cff7ee05d8ef7633e738d1c89021dcf0c5c5',
    detector=('scrfd_10g_bnkps.onnx', '5838f7fe053675b1c7a08b633df49e7af5495cee0493c7dcf6697200b85b5b91'),
    recognizer=('glintr100.onnx', '4ab1d6435d639628a6f3e5008dd4f929edf4c4124b1a7169e1048f9fef534cdf'),
)
FACE_PIPELINE_VERSION = 1
# Changing detection scales, sampling or model files re-extracts every asset.
FACE_PIPELINE = f"{FACE_MODEL['name']}-{FACE_MODEL['recognizer'][1][:8]}-multi640+1920-1s-v{FACE_PIPELINE_VERSION}"

VISUAL_CPU_THREADS = 4
SPEECH_CACHE_BYTES = 256 * 1024 * 1024
FRAME_INTERVAL_SECONDS = 1
SPEECH_CHUNK_SECONDS = 30
SPEECH_LANGUAGE = 'ro'
PIPELINE_VERSION = 1
PIPELINE = (f'siglip2-{VISUAL_MODEL[1][:8]}-whisper-{SPEECH_MODEL[1][:8]}-'
            f'{FRAME_INTERVAL_SECONDS}s-{SPEECH_LANGUAGE}-chunk{SPEECH_CHUNK_SECONDS}-v{PIPELINE_VERSION}')


def configure_cache_environment():
    cache = cache_directory()
    os.environ['HF_HOME'] = str(models_directory() / 'huggingface')
    os.environ['TORCH_HOME'] = str(cache / 'torch')
    os.environ['XDG_CACHE_HOME'] = str(cache)
    os.environ['NUMBA_CACHE_DIR'] = str(cache/'numba')
    os.environ['TORCHINDUCTOR_CACHE_DIR'] = str(cache/'torch-inductor')
    os.environ['TOKENIZERS_PARALLELISM'] = 'false'
    os.environ['HF_HUB_OFFLINE'] = '1'


def cached_model_path(kind):
    from huggingface_hub import snapshot_download
    repository, revision = MODELS[kind]
    return snapshot_download(repository, revision=revision, local_files_only=True,
                              cache_dir=models_directory() / 'huggingface/hub',
                             allow_patterns=['*.json', '*.safetensors', '*.npz', '*.bin', '*.model', '*.txt', '*.jinja'])
