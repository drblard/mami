"""Incremental local inference, using pinned cached models only."""
import json
import math
import os
import subprocess
import tempfile
from pathlib import Path
from app_paths import media_tool
from model_config import (cached_model_path, configure_cache_environment, FRAME_INTERVAL_SECONDS,
                          SPEECH_CACHE_BYTES, SPEECH_CHUNK_SECONDS, SPEECH_LANGUAGE, VISUAL_CPU_THREADS)


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
        configure_cache_environment()
        # mlx-whisper invokes ffmpeg by name; use the signed bundled helper.
        os.environ['PATH'] = str(Path(media_tool('ffmpeg')).parent) + ':' + os.environ.get('PATH', '/usr/bin:/bin')
        self.artifacts = Path(artifacts)
        self.visual = None
        self.speech_model = None
        self.torch_configured = False
        self.audio_streams = {}

    def probe(self, source, kind):
        if kind == 'image':
            result = self.run_command([str(self.image_helper()), '--image-probe', str(source)], text=True, timeout=60)
            return dict(metadata=json.loads(result.stdout), timestamps=[None], speech_times=[], metadataVersion=self.metadata_version)
        from sampling import sample_times, last_frame_time
        from metadata import video_metadata
        result = self.run_command([media_tool('ffprobe'), '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(source)], text=True, timeout=60)
        probe = json.loads(result.stdout)
        metadata = video_metadata(probe)
        length = float(probe.get('format', {}).get('duration', 0))
        end = last_frame_time(probe)
        timestamps = sorted(set(min(t, end) for t in sample_times(length, FRAME_INTERVAL_SECONDS)))
        has_audio = any(s.get('codec_type') == 'audio' for s in probe['streams'])
        return dict(metadata=metadata, timestamps=timestamps, metadataVersion=self.metadata_version,
                    speech_times=list(range(0, math.ceil(length), SPEECH_CHUNK_SECONDS)) if has_audio else [])

    def frame(self, source, target, timestamp):
        if timestamp is None:
            self.run_command([str(self.image_helper()), '--image-frame', str(source), str(target)], timeout=120)
            return
        cmd = [media_tool('ffmpeg'), '-nostdin', '-v', 'error', '-n', '-threads', '1', '-filter_threads', '1']
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

    def frames(self, source, requests):
        """Reuse a native video decoder for a bounded, timestamp-exact batch."""
        if not requests:
            return
        if any(timestamp is None for target, timestamp in requests):
            for target, timestamp in requests:
                self.frame(source, target, timestamp)
            return
        from PIL import Image
        with tempfile.TemporaryDirectory(prefix='preview-batch-', dir=self.artifacts) as directory:
            manifest = Path(directory)/'requests.json'
            manifest.write_text(json.dumps([dict(target=str(target), timestamp=timestamp) for target, timestamp in requests]))
            try:
                result = self.run_command([str(self.image_helper()), '--video-previews', str(source), str(manifest)], text=True, timeout=120)
                actual = json.loads(result.stdout)['actual_times']
                if len(actual) != len(requests) or any(abs(value-timestamp) > .11 for value, (_, timestamp) in zip(actual, requests)):
                    raise ValueError('Native preview timestamps do not match the requested batch')
            except (RuntimeError, subprocess.TimeoutExpired):
                # Unusual codecs/edited end-of-stream behavior retain the existing
                # FFmpeg decoder. Use it only for outputs the native helper did not publish.
                for target, timestamp in requests:
                    if not target.exists():
                        self.frame(source, target, timestamp)
            for target, timestamp in requests:
                with Image.open(target) as image:
                    image.verify()

    @staticmethod
    def model_path(kind):
        return cached_model_path(kind)

    def embedding(self, frame, target, crop=None):
        import numpy as np
        import torch
        from PIL import Image
        if self.visual is None:
            from transformers import AutoModel, AutoProcessor
            if not self.torch_configured:
                # Measured on the target M1 Max: four CPU threads improve visual
                # inference ~1.4x while leaving cores available for the editor.
                torch.set_num_threads(min(VISUAL_CPU_THREADS, os.cpu_count() or 1))
                torch.set_num_interop_threads(1)
                self.torch_configured = True
            path = self.model_path('visual')
            self.visual = (AutoProcessor.from_pretrained(path, local_files_only=True), AutoModel.from_pretrained(path, local_files_only=True).to('cpu').eval())
        processor, model = self.visual
        with Image.open(frame) as image:
            if crop is not None:
                x,y,width,height=crop
                if x<0 or y<0 or width<=0 or height<=0 or x+width>image.width or y+height>image.height:
                    raise ValueError('Invalid packed preview crop')
                image=image.crop((x,y,x+width,y+height))
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
            result = self.run_command([media_tool('ffprobe'), '-v', 'error', '-show_streams', '-of', 'json', key], text=True, timeout=60)
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
        self.run_command([media_tool('ffmpeg'), '-nostdin', '-v', 'error', '-n', '-threads', '1', '-ss', str(start), '-i', key, '-map', f'0:{self.audio_streams[key]}', '-t', str(SPEECH_CHUNK_SECONDS), '-vn', '-ac', '1', '-ar', '16000', str(target)], timeout=120)

    def speech(self, audio, start):
        import mlx_whisper
        import mlx.core as mx
        if self.speech_model is None:
            # MLX otherwise allows tens of GiB of idle GPU buffers on a 64 GiB Mac.
            mx.set_cache_limit(SPEECH_CACHE_BYTES)
            self.speech_model = self.model_path('speech')
        try:
            result = mlx_whisper.transcribe(str(audio), path_or_hf_repo=self.speech_model, language=SPEECH_LANGUAGE, task='transcribe',
                                           word_timestamps=True, condition_on_previous_text=False, verbose=None)
        finally:
            mx.clear_cache()
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
