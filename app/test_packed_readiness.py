import io
import json
import os
from pathlib import Path
import select
import subprocess
import tempfile
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import packed_search_worker as worker


class ReadinessTests(unittest.TestCase):
    def test_coordinator_crash_reaps_private_visual_group(self):
        child=('import os,sys,threading,time,subprocess; sys.path.insert(0,sys.argv[1]); '
               'from packed_search_worker import watch_parent; '
               'subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"]); '
               'watch_parent(threading.Event(),.02); print(os.getpid(),flush=True); time.sleep(30)')
        parent='import subprocess,sys,time; subprocess.Popen([sys.executable,"-u","-c",sys.argv[1],sys.argv[2]],start_new_session=True); time.sleep(30)'
        process=subprocess.Popen([sys.executable,'-u','-c',parent,child,str(Path(worker.__file__).parent)],stdout=subprocess.PIPE)
        group=None
        try:
            self.assertTrue(select.select([process.stdout],[],[],3)[0])
            group=int(process.stdout.readline())
            process.kill();process.wait(timeout=3)
            self.assertTrue(select.select([process.stdout],[],[],3)[0], 'Visual descendant kept its output pipe after the coordinator exited')
            self.assertEqual(process.stdout.read(),b'')
        finally:
            if process.poll() is None:process.kill();process.wait()
            if group:
                try:os.killpg(group,9)
                except ProcessLookupError:pass
            process.stdout.close()

    def client(self, script, **kwargs):
        client=worker.VisualClient(None,None,command=[sys.executable,'-u','-c',script],**kwargs)
        self.addCleanup(client.close)
        return client

    def ready_client(self, script):
        client=self.client('import json,sys; print(json.dumps(dict(ready=True)));\n'+script)
        self.assertTrue(client.read(2)['ready'])
        client.initialized=True
        return client

    def test_stalled_request_write_times_out_and_closes_worker(self):
        client=self.ready_client('import time; time.sleep(10)')
        with patch.multiple(worker,VISUAL_QUERY_TIMEOUT=.05,VISUAL_SHUTDOWN_TIMEOUT=.05,MAX_VISUAL_RESTARTS=0):
            with self.assertRaisesRegex(RuntimeError,'write deadline'):
                client.search('capre',paths=['x'*1024]*1024)
        self.assertIsNotNone(client.process.poll())

    def test_partial_reply_times_out_and_cannot_poison_next_query(self):
        client=self.ready_client('import time; sys.stdin.readline(); sys.stdout.write("{\\\"hits\\\":"); sys.stdout.flush(); time.sleep(10)')
        with patch.multiple(worker,VISUAL_QUERY_TIMEOUT=.05,VISUAL_SHUTDOWN_TIMEOUT=.05,MAX_VISUAL_RESTARTS=0):
            with self.assertRaisesRegex(RuntimeError,'response deadline'):
                client.search('capre')
        self.assertIsNotNone(client.process.poll())
        with self.assertRaisesRegex(RuntimeError,'could not recover'):
            client.search('next query')

    def test_invalid_protocol_is_rejected_and_worker_reaped(self):
        for response in ['not json', '[]', '{}', '{"hits":null}']:
            with self.subTest(response=response):
                client=self.ready_client('sys.stdin.readline(); print('+repr(response)+')')
                with patch.object(worker,'MAX_VISUAL_RESTARTS',0):
                    with self.assertRaisesRegex(RuntimeError,'Visual response'):
                        client.search('capre')
                self.assertIsNotNone(client.process.poll())

    def test_oversized_reply_and_partial_eof_are_rejected(self):
        for response,pattern in [('x'*256,'protocol bound'),('{"hits":','exited')]:
            with self.subTest(pattern=pattern):
                client=self.ready_client('sys.stdin.readline(); sys.stdout.write('+repr(response)+'); sys.stdout.flush()')
                with patch.multiple(worker,VISUAL_REPLY_LIMIT=128,MAX_VISUAL_RESTARTS=0):
                    with self.assertRaisesRegex(RuntimeError,pattern):
                        client.search('capre')
                self.assertIsNotNone(client.process.poll())

    def test_oversized_request_does_not_write_or_restart(self):
        client=self.ready_client('for line in sys.stdin: print(json.dumps(dict(hits=[])))')
        with patch.object(worker,'VISUAL_REQUEST_LIMIT',128):
            with self.assertRaisesRegex(ValueError,'request exceeded'):
                client.search('x'*256)
        self.assertEqual(client.search('small'),([],False))
        self.assertEqual(client.restarts,0)

    def test_startup_deadline_uses_controllable_clock(self):
        now=[0.0]
        client=self.client('import time; time.sleep(10)',clock=lambda:now[0])
        self.assertEqual(client.search('capre'),([],True))
        now[0]=worker.VISUAL_STARTUP_TIMEOUT
        with patch.multiple(worker,MAX_VISUAL_RESTARTS=0,VISUAL_SHUTDOWN_TIMEOUT=.05):
            with self.assertRaisesRegex(RuntimeError,'initialization deadline'):
                client.search('capre')
        self.assertIsNotNone(client.process.poll())

    def test_repeated_process_failure_exhausts_exact_restart_budget(self):
        client=self.client('import sys; sys.exit(2)')
        deadline=time.monotonic()+3
        failure=None
        while time.monotonic()<deadline:
            try:client.search('capre')
            except RuntimeError as error:
                failure=str(error)
                break
            time.sleep(.01)
        self.assertIsNotNone(failure)
        self.assertEqual(client.restarts,worker.MAX_VISUAL_RESTARTS)
        self.assertIsNotNone(client.process.poll())
        with self.assertRaisesRegex(RuntimeError,'could not recover'):
            client.search('again')
        self.assertEqual(client.restarts,worker.MAX_VISUAL_RESTARTS)

    def test_shutdown_closes_encoder_descendant_pipe(self):
        script=('import subprocess; subprocess.Popen([sys.executable,"-c","import time; time.sleep(10)"]); '
                'print(json.dumps(dict(spawned=True))); sys.stdin.read()')
        client=self.ready_client(script)
        self.assertTrue(client.read(2)['spawned'])
        descriptor=os.dup(client.process.stdout.fileno())
        try:
            client.close()
            self.assertTrue(select.select([descriptor],[],[],2)[0], 'Descendant retained the output pipe')
            self.assertEqual(os.read(descriptor,1),b'')
        finally:os.close(descriptor)

    def test_text_queries_do_not_wait_for_visual_initialization(self):
        self.check_text_readiness(fail_visual=False)

    def test_text_queries_remain_available_when_visual_initialization_fails(self):
        self.check_text_readiness(fail_visual=True)

    def test_combined_queries_preserve_speech_when_visual_service_fails(self):
        self.check_text_readiness(fail_visual=True,mode='both')

    def check_text_readiness(self, fail_visual, mode='speech'):
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
            samples=0
            def __init__(self,*args):pass
            def search(self,*args):
                if fail_visual:raise RuntimeError('Visual initialization failed')
                raise TimeoutError('Visual initialization has not completed')
            def close(self):pass
        output = io.StringIO()
        def requests():
            yield json.dumps(dict(query='capre', mode=mode))+'\n'
            replies = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertTrue(replies[0]['ready'])
            self.assertFalse(replies[0]['visual_ready'])
            self.assertEqual(replies[1]['hits'][0]['path'], 'clip')
            self.assertNotIn('error',replies[1])
            self.assertEqual(replies[1]['indexed_samples'],0 if mode=='both' else 1)
            if mode=='both':
                self.assertEqual(replies[1]['visual_error'],'Visual initialization failed')
                self.assertFalse(replies[1]['visual_pending'])
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
