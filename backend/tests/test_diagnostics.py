from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from kiwibit_hub.diagnostics import redact


class DiagnosticTests(unittest.TestCase):
    def run_child(self, body):
        with tempfile.TemporaryDirectory() as folder:
            code = ('from kiwibit_hub.diagnostics import Diagnostics\n'
                    'import sys\n'
                    'd = Diagnostics(sys.argv[1], console=False)\n' + body)
            result = subprocess.run([sys.executable, '-c', code, folder],
                                    cwd=Path(__file__).resolve().parent.parent,
                                    capture_output=True, text=True, timeout=20)
            logs = {p.name: p.read_text(encoding='utf-8') for p in Path(folder).glob('*.log')}
            return result, logs

    def test_unhandled_exception_and_redaction(self):
        result, logs = self.run_child(
            "raise RuntimeError('example https://example.com/?token=secret Bearer secret')\n")
        self.assertNotEqual(result.returncode, 0)
        log = next(text for name, text in logs.items() if not name.endswith('-stacks.log'))
        self.assertIn('Unhandled Python exception', log)
        self.assertIn('RuntimeError', log)
        self.assertNotIn('secret', log)

    def test_thread_exception_and_stack_watchdog(self):
        result, logs = self.run_child(
            'import threading, faulthandler, time\n'
            'def fail():\n    raise ValueError("thread-test")\n'
            't = threading.Thread(target=fail)\nt.start()\nt.join()\n'
            'faulthandler.dump_traceback_later(0.1, file=d.stacks)\ntime.sleep(0.3)\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any('Unhandled thread exception' in text for text in logs.values()))
        self.assertTrue(any('Timeout' in text and '<module>' in text
                            for name, text in logs.items() if name.endswith('-stacks.log')))

    def test_redact_token_fields(self):
        self.assertNotIn('sensitive', redact('"access_token": "sensitive"'))


if __name__ == '__main__':
    unittest.main()
