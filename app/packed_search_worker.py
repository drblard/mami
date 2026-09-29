"""Persistent search with immediate SQLite text results and independently warmed visuals."""
from functools import lru_cache
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import threading
import time

from packed_vectors import PackedIndex
from search_logic import combine_hits
from search_store import SearchStore
from text_encoder import NativeEncoder
from vector_overlay import VectorOverlay
from vector_sync import resolve_generation

VISUAL_REPLY_LIMIT = 8 * 1024 * 1024
VISUAL_QUERY_TIMEOUT = 10
VISUAL_STARTUP_TIMEOUT = 30
VISUAL_REQUEST_LIMIT = 8 * 1024 * 1024
VISUAL_SHUTDOWN_TIMEOUT = 2
MAX_VISUAL_RESTARTS = 2


class VisualClient:
    """A separate interpreter keeps MLX/tokenizer initialization off the text path."""
    def __init__(self, args, executable, command=None, clock=time.monotonic):
        if command is None:
            command = [sys.executable, '-B', str(Path(__file__).with_name('search_worker.py')),
                       '--index', args.index, '--packed-index', str(args.packed_index),
                       '--projection', str(args.projection), '--native-encoder', str(args.native_encoder),
                       '--native-executable', str(executable), '--visual-service']
        self.command = command
        self.clock = clock
        self.failure = None
        self._start()
        self.restarts = 0
        self.buffer = bytearray()
        self.initialized = False
        self.samples = None

    def _start(self):
        self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        bufsize=0, start_new_session=True)
        os.set_blocking(self.process.stdin.fileno(), False)
        self.startup_deadline = self.clock() + VISUAL_STARTUP_TIMEOUT

    def read(self, timeout):
        deadline = self.clock() + timeout
        while b'\n' not in self.buffer:
            if len(self.buffer) > VISUAL_REPLY_LIMIT:
                raise RuntimeError('Visual response exceeded the protocol bound')
            remaining = max(0, deadline-self.clock())
            if timeout > 0 and remaining == 0:
                return None
            if not select.select([self.process.stdout], [], [], remaining)[0]:
                return None
            data = os.read(self.process.stdout.fileno(), 65536)
            if not data:
                raise RuntimeError('Visual search process exited')
            self.buffer.extend(data)
        line, _, remaining = self.buffer.partition(b'\n')
        self.buffer = bytearray(remaining)
        if len(line) > VISUAL_REPLY_LIMIT:
            raise RuntimeError('Visual response exceeded the protocol bound')
        try:
            response = json.loads(line)
        except (ValueError, UnicodeError) as error:
            raise RuntimeError('Visual response was not valid JSON') from error
        if not isinstance(response, dict):
            raise RuntimeError('Visual response must be an object')
        if 'error' in response:
            raise RuntimeError(response['error'])
        return response

    def search(self, query, paths=None, scope=None):
        if self.failure is not None:
            raise RuntimeError(self.failure)
        try:
            return self._search(query, paths, scope)
        except (OSError, RuntimeError, TimeoutError) as error:
            self.close()
            if self.restarts >= MAX_VISUAL_RESTARTS:
                self.failure = f'Visual search could not recover: {error}'
                raise RuntimeError(self.failure) from error
            self.restarts += 1
            self._start()
            self.buffer.clear()
            self.initialized = False
            self.samples = None
            print(f'Restarting visual search after: {error}', file=sys.stderr, flush=True)
            return [], True

    def _search(self, query, paths=None, scope=None):
        if not self.initialized:
            response = self.read(0)
            if response is None:
                if self.clock() >= self.startup_deadline:
                    raise TimeoutError('Visual initialization deadline expired')
                return [], True
            if response.get('ready') is not True:
                raise RuntimeError('Visual process did not announce readiness')
            self.initialized = True
            self.samples = response.get('samples')
        request = dict(query=query, scope=scope or {})
        if paths is not None:
            request['paths'] = list(paths)
        data = memoryview((json.dumps(request)+'\n').encode())
        if len(data) > VISUAL_REQUEST_LIMIT:
            raise ValueError('Visual request exceeded the protocol bound')
        deadline = self.clock() + VISUAL_QUERY_TIMEOUT
        while data:
            remaining = max(0, deadline-self.clock())
            if remaining == 0 or not select.select([], [self.process.stdin], [], remaining)[1]:
                raise TimeoutError('Visual request write deadline expired')
            try:
                written = os.write(self.process.stdin.fileno(), data[:65536])
            except BlockingIOError:
                continue
            data = data[written:]
        response = self.read(max(0, deadline-self.clock()))
        if response is None:
            raise TimeoutError('Visual search response deadline expired')
        if not isinstance(response.get('hits'), list):
            raise RuntimeError('Visual response is missing its hit list')
        self.samples = response.get('samples', self.samples)
        return response['hits'], False

    def close(self):
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=VISUAL_SHUTDOWN_TIMEOUT)
        except subprocess.TimeoutExpired:
            self._kill_group()
            self.process.wait()
        # An encoder may outlive a failed visual parent; it belongs to this
        # private process group, never to the live app's other workers.
        self._kill_group()
        self.process.stdout.close()

    def _kill_group(self):
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


class VisualRuntime:
    """One background initializer/updater; queries pin one complete generation."""
    def __init__(self, args, executable):
        self.args = args
        self.executable = executable
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.lock = threading.Lock()
        self.state = {}
        self.error = None
        self.encoder = None
        self.last_samples = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        index = None
        try:
            with SearchStore(self.args.projection, read_only=True) as reader:
                generation = resolve_generation(self.args.packed_index)
                index = PackedIndex(generation)
                self.encoder = NativeEncoder(self.executable, self.args.native_encoder)
                self.encode = lru_cache(maxsize=128)(self.encoder.encode)
                overlay = VectorOverlay(index, reader)
                overlay.refresh()
                warm_vector = self.encode('a photo')
                index.search(warm_vector, limit=1)
                self.state.update(index=index, overlay=overlay, path=generation)
                self.ready.set()
                while not self.stop.wait(1):
                    replacement = None
                    try:
                        latest = resolve_generation(self.args.packed_index)
                        if latest == self.state['path']:
                            self.state['overlay'].refresh(reader)
                            continue
                        replacement = PackedIndex(latest)
                        next_overlay = VectorOverlay(replacement, reader)
                        next_overlay.refresh(reader)
                        replacement.search(warm_vector, limit=1)
                        if self.stop.is_set():
                            replacement.close()
                            return
                        with self.lock:
                            previous = self.state['index']
                            self.state.update(index=replacement, overlay=next_overlay, path=latest)
                            replacement = None
                            previous.close()
                    except Exception as error:
                        if replacement is not None:
                            replacement.close()
                        print(f'Vector delta refresh: {error}', file=sys.stderr, flush=True)
        except Exception as error:
            self.error = str(error)
            self.ready.set()
        finally:
            with self.lock:
                current = self.state.get('index', index)
                if current is not None:
                    current.close()
            if self.encoder is not None:
                self.encoder.close()

    def search(self, query, paths=None, scope=None):
        if not self.ready.is_set():
            return [], True
        if self.error:
            raise RuntimeError(self.error)
        feature = self.encode(query)
        with self.lock:
            hits = self.state['overlay'].search(feature, paths=paths, scope=scope)
            self.last_samples = self.state['index'].manifest['rows']
            return hits, False

    def close(self):
        self.stop.set()
        self.thread.join(timeout=15)


def run(args):
    if not args.native_encoder or not args.projection:
        raise ValueError('Packed search requires a native encoder and ready SQLite projection')
    executable = Path(args.native_executable) if args.native_executable else Path(__file__).resolve().parent.parent/'MacOS/Mami'
    if getattr(args, 'visual_service', False):
        runtime = VisualRuntime(args, executable)
        try:
            if not runtime.ready.wait(VISUAL_STARTUP_TIMEOUT):
                raise TimeoutError('Visual initialization deadline expired')
            if runtime.error:
                raise RuntimeError(runtime.error)
            print(json.dumps(dict(ready=True, samples=runtime.state['index'].manifest['rows'])), flush=True)
            for line in sys.stdin:
                try:
                    request = json.loads(line)
                    hits, _ = runtime.search(request['query'], request.get('paths'), request.get('scope'))
                    response = dict(hits=hits, samples=runtime.last_samples)
                except Exception as error:
                    response = dict(error=str(error))
                print(json.dumps(response), flush=True)
        finally:
            runtime.close()
        return
    generation = resolve_generation(args.packed_index)
    manifest = json.loads((generation/'manifest.json').read_text())
    with SearchStore(args.projection, read_only=True) as projection:
        checkpoint = projection.db.execute('SELECT identity,epoch FROM checkpoint WHERE id=1 AND ready=1').fetchone()
        if checkpoint is None or checkpoint['identity'] != manifest['source_identity'] or checkpoint['epoch'] != manifest['epoch']:
            raise ValueError('Packed index and catalog projection identities differ')
        visual_runtime = VisualClient(args, executable)
        try:
            print(json.dumps(dict(ready=True, visual_ready=False, samples=manifest['rows'])), flush=True)
            for line in sys.stdin:
                started = time.monotonic()
                try:
                    request = json.loads(line)
                    query = request['query'].strip()
                    if not query or len(query) > 2000:
                        raise ValueError('Query must contain between 1 and 2000 characters')
                    mode = request.get('mode', 'both')
                    if mode not in ('visual', 'speech', 'both'):
                        raise ValueError('Unknown search mode')
                    allowed = set(request['paths']) if 'paths' in request else None
                    scope = request.get('scope') or {}
                    visual, pending, visual_error = [], False, None
                    if mode != 'speech':
                        try:
                            visual, pending = visual_runtime.search(query, allowed, scope)
                        except (OSError, RuntimeError, ValueError) as error:
                            if mode == 'visual':
                                raise
                            visual_error = str(error)
                    for hit in visual:
                        frame = projection.db.execute('SELECT frame,crop FROM frames WHERE asset=? AND ordinal=?', (hit['asset'], hit['ordinal'])).fetchone()
                        if frame:
                            hit['frame'] = frame['frame']
                            hit['crop'] = json.loads(frame['crop'] or 'null')
                    spoken = []
                    if mode != 'visual':
                        for hit in projection.speech(query, allowed_paths=allowed, scope=scope):
                            spoken.append(dict(path=hit['path'], kind=hit['kind'], timestamp=hit['timestamp'],
                                               frame=hit['frame'] or '', crop=hit.get('crop'), evidence=hit['evidence'], score=1.0))
                    hits = visual if mode == 'visual' else spoken if mode == 'speech' else combine_hits(visual, spoken)
                    indexed_samples=getattr(visual_runtime, 'samples', None)
                    reply = dict(hits=hits, visual_pending=pending, elapsed=time.monotonic()-started,
                                 indexed_samples=manifest['rows'] if indexed_samples is None else indexed_samples)
                    if visual_error is not None:
                        reply['visual_error'] = visual_error
                except Exception as error:
                    reply = dict(error=str(error))
                print(json.dumps(reply, ensure_ascii=False), flush=True)
        finally:
            visual_runtime.close()
