import subprocess
import sys
import unittest

from text_encoder import NativeEncoder


class NativeProtocolTests(unittest.TestCase):
    def encoder(self, script):
        value = NativeEncoder.__new__(NativeEncoder)
        value.process = subprocess.Popen([sys.executable,'-u','-c',script],stdin=subprocess.PIPE,stdout=subprocess.PIPE)
        value.buffer = bytearray()
        self.addCleanup(value.close)
        return value

    def test_partial_native_output_has_a_deadline(self):
        value=self.encoder('import sys,time;sys.stdout.write("partial");sys.stdout.flush();time.sleep(10)')
        with self.assertRaises(TimeoutError):
            value.read(.05)

    def test_multiple_replies_preserve_framing(self):
        value=self.encoder('print(\'{"ready":true}\\n{"embedding":[1,2]}\')')
        self.assertEqual(value.read(2),dict(ready=True))
        self.assertEqual(value.read(2),dict(embedding=[1,2]))

    def test_native_error_is_reported_without_waiting_for_more_output(self):
        value=self.encoder('print(\'{"error":"invalid model"}\')')
        with self.assertRaisesRegex(RuntimeError,'invalid model'):
            value.read(2)


if __name__=='__main__':
    unittest.main()
