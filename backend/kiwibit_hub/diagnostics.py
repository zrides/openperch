"""Local, per-run diagnostics for the Bird Library (no model or account access)."""
from __future__ import annotations

import atexit
import faulthandler
import logging
import os
from pathlib import Path
import re
import sys
import threading
import time


def redact(text):
    text = re.sub(r'https?://[^\s\'"<>]+', '[URL redacted]', text)
    text = re.sub(r'(?i)\bBearer\s+\S+', 'Bearer [redacted]', text)
    return re.sub(r'(?i)(\b(?:access_token|refresh_token|token|authorization)\b[\s\"\x27:=]+)[^\s,}\"\x27]+',
                  r'\1[redacted]', text)


class SafeFormatter(logging.Formatter):
    def format(self, record):
        return redact(super().format(record))


class ErrorStream:
    """Collect complete lines so credentials split across writes are still redacted."""
    encoding = 'utf-8'

    def __init__(self, logger):
        self.logger = logger
        self.pending = ''
        self.lock = threading.RLock()

    def write(self, text):
        with self.lock:
            self.pending += text
            while '\n' in self.pending:
                line, self.pending = self.pending.split('\n', 1)
                if line.strip():
                    self.logger.warning('stderr: %s', line)
        return len(text)

    def flush(self):
        with self.lock:
            if self.pending:
                self.logger.warning('stderr: %s', self.pending)
                self.pending = ''

    def isatty(self):
        return False


class Diagnostics:
    def __init__(self, folder):
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        stem = time.strftime('%Y%m%d-%H%M%S') + f'-{os.getpid()}'
        self.path = folder / f'{stem}.log'
        self.logger = logging.getLogger('bird-library')
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        handler = logging.FileHandler(self.path, encoding='utf-8')
        handler.setFormatter(SafeFormatter('%(asctime)s %(levelname)s [%(threadName)s] %(message)s'))
        self.logger.addHandler(handler)
        # Keep this file open for the lifetime of the process. faulthandler writes
        # directly to its descriptor, including when Python cannot acquire the GIL.
        self.stacks = (folder / f'{stem}-stacks.log').open('a', encoding='utf-8', buffering=1)
        faulthandler.enable(file=self.stacks, all_threads=True)
        self.original_stderr = sys.stderr
        sys.stderr = ErrorStream(self.logger)
        sys.excepthook = lambda kind, value, tb: self.logger.critical(
            'Unhandled Python exception', exc_info=(kind, value, tb))
        threading.excepthook = lambda args: self.logger.critical(
            'Unhandled thread exception: %s', args.thread.name,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        self.last_status = 0.0
        self.logger.info('SESSION START pid=%s Python=%s executable=%s',
                         os.getpid(), sys.version.replace('\n', ' '), sys.executable)
        atexit.register(self.on_exit)

    def attach_qt(self, app):
        from PySide6.QtCore import QTimer, qInstallMessageHandler, qVersion
        self.qt_handler = lambda kind, context, message: self.logger.warning('Qt %s: %s', kind, message)
        qInstallMessageHandler(self.qt_handler)
        self.logger.info('Qt %s; UI watchdog enabled (60 seconds without a heartbeat)', qVersion())
        self.timer = QTimer(app)
        self.timer.timeout.connect(self.heartbeat)
        self.timer.start(5000)
        self.heartbeat()

    def heartbeat(self):
        faulthandler.dump_traceback_later(60, repeat=True, file=self.stacks)

    def status(self, text):
        now = time.monotonic()
        if now - self.last_status >= 10:
            self.logger.info('STATUS %s', text)
            self.last_status = now

    def on_exit(self):
        faulthandler.cancel_dump_traceback_later()
        sys.stderr.flush()
        self.logger.info('PYTHON EXIT (see preceding messages for errors)')


def install():
    return Diagnostics(Path(__file__).resolve().parent.parent / 'bird-library' / 'diagnostics')
