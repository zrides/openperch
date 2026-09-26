import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from diagnostics import redact


class DiagnosticTests(unittest.TestCase):
    def run_child(self, body):
        with tempfile.TemporaryDirectory() as folder:
            code = ('from diagnostics import Diagnostics\n'
                    'import sys\n'
                    'd = Diagnostics(sys.argv[1])\n' + body)
            result = subprocess.run([sys.executable, '-c', code, folder],
                                    cwd=Path(__file__).resolve().parent,
                                    env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen'},
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

    def test_qt_messages_and_clean_exit(self):
        result, logs = self.run_child(
            'from PySide6.QtWidgets import QApplication\n'
            'from PySide6.QtCore import QTimer, qWarning\n'
            'app = QApplication([])\nd.attach_qt(app)\n'
            'qWarning("diagnostic-qt-test")\n'
            'QTimer.singleShot(50, app.quit)\napp.exec()\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any('diagnostic-qt-test' in text and 'PYTHON EXIT' in text for text in logs.values()))

    def test_redact_token_fields(self):
        self.assertNotIn('sensitive', redact('"access_token": "sensitive"'))


if __name__ == '__main__':
    unittest.main()
