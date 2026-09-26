"""Incremental local inference, using pinned cached models only."""
import json
import math
import os
import subprocess
import uuid
from pathlib import Path


class Backend:
    metadata_version = 2

    @staticmethod
    def run_command(command, **kwargs):
        try:
            return subprocess.run(command, check=True, capture_output=True, **kwargs)
        except subprocess.CalledProcessError as error:
            detail = error.stderr.decode(errors='replace') if isinstance(error.stderr, bytes) else error.stderr
            raise RuntimeError(f'{Path(command[0]).name}: {(detail or str(error)).strip()[-1800:]}') from error

    @staticmethod
    def image_helper():
        return Path(__file__).resolve().parent.parent / 'MacOS/Mami'
    def __init__(self, artifacts):
        # mlx-whisper invokes ffmpeg by name when reading saved audio. Finder-
        # launched apps do not inherit Homebrew's bin directory from a shell.
        os.environ['PATH'] = '/opt/homebrew/bin:' + os.environ.get('PATH', '/usr/bin:/bin')
        self.artifacts = Path(artifacts)
        self.visual = None
        self.speech_model = None
        self.torch_configured = False
        self.audio_streams = {}

    def probe(self, source, kind):
        if kind == 'image':
            result = self.run_command([str(self.image_helper()), '--image-probe', str(source)], text=True, timeout=60)
            return dict(metadata=json.loads(result.stdout), timestamps=[None], speech_times=[], metadataVersion=self.metadata_version)
        from lab import sample_times, last_frame_time
        from metadata import export_metadata
        result = self.run_command(['/opt/homebrew/bin/ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(source)], text=True, timeout=60)
        probe = json.loads(result.stdout)
        record = dict(path=source.name, kind=kind, probe=probe)
        self.artifacts.mkdir(parents=True, exist_ok=True)
        inventory = self.artifacts / (str(uuid.uuid4()) + '-metadata.json')
        with inventory.open('x') as output:
            json.dump(dict(root=str(source.parent), files=[record]), output)
        metadata = export_metadata(inventory)[source.name]
        length = float(probe.get('format', {}).get('duration', 0))
        timestamps = [None] if kind == 'image' else sorted(set(min(t, last_frame_time(record)) for t in sample_times(length, 1)))
        has_audio = any(s.get('codec_type') == 'audio' for s in probe['streams'])
        return dict(metadata=metadata, timestamps=timestamps, metadataVersion=self.metadata_version,
                    speech_times=list(range(0, math.ceil(length), 30)) if kind == 'video' and has_audio else [])

    def frame(self, source, target, timestamp):
        if timestamp is None:
            self.run_command([str(self.image_helper()), '--image-frame', str(source), str(target)], timeout=120)
            return
        cmd = ['/opt/homebrew/bin/ffmpeg', '-nostdin', '-v', 'error', '-n', '-threads', '1', '-filter_threads', '1']
        if timestamp is not None:
            cmd += ['-ss', str(timestamp)]
        cmd += ['-i', str(source), '-frames:v', '1', '-vf', 'scale=640:640:force_original_aspect_ratio=decrease:out_range=full', '-pix_fmt', 'yuvj420p', '-q:v', '3', '-threads', '1', str(target)]
        self.run_command(cmd, timeout=120)
        if not target.exists() and timestamp is not None:
            # Some edited/variable-rate videos advertise a duration slightly
            # past their final decodable frame. FFmpeg then exits successfully
            # without writing a JPEG. Sample within the final half-second.
            retry = list(cmd)
            retry[retry.index('-ss') + 1] = str(max(0, timestamp - .5))
            self.run_command(retry, timeout=120)
        if not target.exists():
            raise RuntimeError(f'No preview frame could be decoded at {timestamp}s in {source.name}')
        from PIL import Image
        with Image.open(target) as image:
            image.verify()

    @staticmethod
    def model_path(kind):
        from huggingface_hub import snapshot_download
        from lab import LAB, MODELS
        repo, revision = MODELS[kind]
        return snapshot_download(repo, revision=revision, local_files_only=True, cache_dir=LAB / 'cache/huggingface/hub',
                                 allow_patterns=['*.json', '*.safetensors', '*.npz', '*.bin', '*.model', '*.txt', '*.jinja'])

    def embedding(self, frame, target):
        import numpy as np
        import torch
        from PIL import Image
        if self.visual is None:
            from transformers import AutoModel, AutoProcessor
            if not self.torch_configured:
                torch.set_num_threads(1)
                torch.set_num_interop_threads(1)
                self.torch_configured = True
            path = self.model_path('visual')
            self.visual = (AutoProcessor.from_pretrained(path, local_files_only=True), AutoModel.from_pretrained(path, local_files_only=True).to('cpu').eval())
        processor, model = self.visual
        with Image.open(frame) as image:
            inputs = processor(images=image.convert('RGB'), return_tensors='pt')
        with torch.inference_mode():
            feature = model.get_image_features(**inputs)
            if not isinstance(feature, torch.Tensor):
                feature = feature.pooler_output
            feature = torch.nn.functional.normalize(feature.float(), dim=-1).cpu().numpy()[0]
        with target.open('xb') as output:
            np.save(output, feature)

    def audio(self, source, target, start):
        key = str(source)
        if key not in self.audio_streams:
            result = self.run_command(['/opt/homebrew/bin/ffprobe', '-v', 'error', '-show_streams', '-of', 'json', key], text=True, timeout=60)
            # iPhone spatial recordings also contain a regular stereo track.
            # Automatic selection favors the four-channel APAC stream, for
            # which FFmpeg has no decoder. Preserve the original; select the
            # conventional track only for the transcription working copy.
            streams = [s for s in json.loads(result.stdout)['streams']
                       if s.get('codec_type') == 'audio' and s.get('codec_name') not in (None, 'apple_apac')]
            if not streams:
                raise RuntimeError(f'No compatible transcription audio track in {source.name}')
            stream = max(streams, key=lambda s: s.get('disposition', {}).get('default', 0))
            self.audio_streams[key] = stream['index']
        self.run_command(['/opt/homebrew/bin/ffmpeg', '-nostdin', '-v', 'error', '-n', '-threads', '1', '-ss', str(start), '-i', key, '-map', f'0:{self.audio_streams[key]}', '-t', '30', '-vn', '-ac', '1', '-ar', '16000', str(target)], timeout=120)

    def speech(self, audio, start):
        import mlx_whisper
        if self.speech_model is None:
            self.speech_model = self.model_path('speech')
        result = mlx_whisper.transcribe(str(audio), path_or_hf_repo=self.speech_model, language='ro', task='transcribe',
                                       word_timestamps=True, condition_on_previous_text=False, verbose=None)
        segments = []
        for segment in result.get('segments', []):
            value = dict(segment)
            value['start'] += start
            value['end'] += start
            for word in value.get('words', []):
                word['start'] += start
                word['end'] += start
            segments.append(value)
        return segments
