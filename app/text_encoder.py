"""Persistent native query encoder, with bounded protocol reads and cleanup."""
import json
from pathlib import Path
import select
import subprocess
import time

from model_config import VISUAL_MODEL

TOKEN_COUNT = 64
MAX_REPLY_BYTES = 128 * 1024
STARTUP_TIMEOUT_SECONDS = 10
QUERY_TIMEOUT_SECONDS = 5


class NativeEncoder:
    def __init__(self, executable, artifact):
        from tokenizers import Tokenizer
        artifact = Path(artifact)
        manifest = json.loads((artifact/'manifest.json').read_text())
        if tuple(manifest['model']) != VISUAL_MODEL or manifest['token_count'] != TOKEN_COUNT:
            raise ValueError('Compiled query encoder does not match the pinned visual index')
        self.tokenizer = Tokenizer.from_file(str(artifact/'tokenizer.json'))
        self.tokenizer.enable_truncation(max_length=TOKEN_COUNT)
        self.tokenizer.enable_padding(length=TOKEN_COUNT,pad_id=0,pad_token='<pad>')
        self.process = subprocess.Popen([str(executable),'--encode-text',str(artifact/'SiglipText.mlmodelc')],
                                        stdin=subprocess.PIPE,stdout=subprocess.PIPE)
        self.buffer = bytearray()
        try:
            if not self.read(STARTUP_TIMEOUT_SECONDS).get('ready'):
                raise ValueError('Native encoder did not become ready')
        except BaseException:
            self.close()
            raise

    def read(self, timeout):
        import os
        deadline = time.monotonic()+timeout
        while b'\n' not in self.buffer:
            if len(self.buffer) > MAX_REPLY_BYTES:
                raise ValueError('Native encoder reply exceeded size limit')
            remaining = deadline-time.monotonic()
            if remaining <= 0 or not select.select([self.process.stdout],[],[],remaining)[0]:
                raise TimeoutError('Native encoder response deadline expired')
            chunk = os.read(self.process.stdout.fileno(),4096)
            if not chunk:
                raise RuntimeError('Native encoder closed its output')
            self.buffer.extend(chunk)
        line, _, rest = self.buffer.partition(b'\n')
        self.buffer = bytearray(rest)
        if len(line) > MAX_REPLY_BYTES:
            raise ValueError('Native encoder reply exceeded size limit')
        response = json.loads(line)
        if 'error' in response:
            raise RuntimeError(response['error'])
        return response

    def encode(self, query):
        import numpy as np
        tokens = self.tokenizer.encode(query).ids
        try:
            self.process.stdin.write((json.dumps(dict(input_ids=tokens))+'\n').encode())
            self.process.stdin.flush()
            result = np.asarray(self.read(QUERY_TIMEOUT_SECONDS)['embedding'], dtype=np.float32)
            if result.shape != (768,) or not np.isfinite(result).all():
                raise ValueError('Invalid native text embedding')
            norm = np.linalg.norm(result)
            if norm == 0:
                raise ValueError('Empty native text embedding')
            return result/norm
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.process.stdin:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill(); self.process.wait()
        if self.process.stdout:
            self.process.stdout.close()
