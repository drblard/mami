import io
import json
from pathlib import Path
import tempfile
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import packed_search_worker as worker


class ReadinessTests(unittest.TestCase):
    def test_text_queries_do_not_wait_for_visual_initialization(self):
        self.check_text_readiness(fail_visual=False)

    def test_text_queries_remain_available_when_visual_initialization_fails(self):
        self.check_text_readiness(fail_visual=True)

    def check_text_readiness(self, fail_visual):
        class Projection:
            def __init__(self, *args, **kwargs): self.db = self
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def execute(self, *args): return self
            def fetchone(self): return dict(identity='fixture', epoch='epoch')
            def speech(self, *args, **kwargs):
                return [dict(path='clip', kind='video', timestamp=1, frame='cached', evidence='capre')]
        manifest = dict(source_identity='fixture', epoch='epoch', rows=1)
        class Visual:
            def __init__(self,*args):pass
            def search(self,*args):
                if fail_visual:raise RuntimeError('Visual initialization failed')
                raise TimeoutError('Visual initialization has not completed')
            def close(self):pass
        output = io.StringIO()
        def requests():
            yield json.dumps(dict(query='capre', mode='speech'))+'\n'
            replies = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertTrue(replies[0]['ready'])
            self.assertFalse(replies[0]['visual_ready'])
            self.assertEqual(replies[1]['hits'][0]['path'], 'clip')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'manifest.json').write_text(json.dumps(manifest))
            args = SimpleNamespace(native_encoder='artifact', native_executable='helper', projection='projection', packed_index=root)
            with patch.multiple(worker, SearchStore=Projection, VisualClient=Visual, resolve_generation=lambda _: root), \
                 patch.object(worker.sys, 'stdin', requests()), patch.object(worker.sys, 'stdout', output):
                worker.run(args)

    def test_visual_client_does_not_wait_for_an_unready_process(self):
        command=[sys.executable,'-u','-c','import time; time.sleep(10)']
        client=worker.VisualClient(None,None,command=command)
        try:
            self.assertEqual(client.search('capre'),([],True))
        finally:client.close()

    def test_visual_client_reads_ready_then_queries_without_protocol_drift(self):
        script='import json,sys; print(json.dumps(dict(ready=True,samples=7)));\nfor line in sys.stdin: print(json.dumps(dict(hits=[],samples=8)))'
        client=worker.VisualClient(None,None,command=[sys.executable,'-u','-c',script])
        try:
            deadline=time.monotonic()+2
            while time.monotonic()<deadline:
                hits,pending=client.search('capre')
                if not pending:break
                time.sleep(.01)
            self.assertFalse(pending)
            self.assertEqual(hits,[])
            self.assertEqual(client.samples,8)
        finally:client.close()

    def test_visual_process_exit_recovers_without_restarting_text_service(self):
        with tempfile.TemporaryDirectory() as directory:
            marker=str(Path(directory)/'first-start')
            script=('import json,sys; from pathlib import Path; p=Path(sys.argv[1]);\n'
                    'if not p.exists(): p.write_text("attempted"); sys.exit(2)\n'
                    'print(json.dumps(dict(ready=True,samples=7)))\n'
                    'for line in sys.stdin: print(json.dumps(dict(hits=[],samples=8)))')
            client=worker.VisualClient(None,None,command=[sys.executable,'-u','-c',script,marker])
            try:
                deadline=time.monotonic()+3
                pending=True
                while time.monotonic()<deadline:
                    _,pending=client.search('capre')
                    if not pending:break
                    time.sleep(.01)
                self.assertFalse(pending)
                self.assertEqual(client.restarts,1)
                self.assertEqual(client.samples,8)
            finally:client.close()


if __name__ == '__main__': unittest.main()
