"""Per-run diagnostics: redacted activity log, crash hooks and fault stacks."""
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

KEEP_RUNS = 20


def redact(text):
    text = re.sub(r'https?://[^\s\'"<>]+', '[URL redacted]', text)
    text = re.sub(r'(?i)\bBearer\s+\S+', 'Bearer [redacted]', text)
    return re.sub(r'(?i)(\b(?:access_token|refresh_token|token|authorization)\b[\s\"\x27:=]+)[^\s,}\"\x27]+',
                  r'\1[redacted]', text)


class SafeFormatter(logging.Formatter):
    def format(self, record):
        return redact(super().format(record))


class Diagnostics:
    def __init__(self, folder, console=True):
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        self._prune(folder)
        stem = time.strftime('%Y%m%d-%H%M%S') + f'-{os.getpid()}'
        self.path = folder / f'{stem}.log'
        self.logger = logging.getLogger('kiwibit_hub')
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        formatter = SafeFormatter('%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s')
        handlers = [logging.FileHandler(self.path, encoding='utf-8')]
        if console:
            handlers.append(logging.StreamHandler(sys.stderr))
        for handler in handlers:
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)
        # Kept open for the process lifetime: faulthandler writes to the raw descriptor.
        self.stacks = (folder / f'{stem}-stacks.log').open('a', encoding='utf-8', buffering=1)
        faulthandler.enable(file=self.stacks, all_threads=True)
        sys.excepthook = lambda kind, value, tb: self.logger.critical(
            'Unhandled Python exception', exc_info=(kind, value, tb))
        threading.excepthook = lambda args: self.logger.critical(
            'Unhandled thread exception: %s', args.thread.name,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        self.logger.info('SESSION START pid=%s Python=%s', os.getpid(), sys.version.replace('\n', ' '))
        atexit.register(self.on_exit)

    @staticmethod
    def _prune(folder):
        runs = sorted(folder.glob('*-stacks.log'))
        for stacks in runs[:-KEEP_RUNS]:
            stacks.unlink(missing_ok=True)
            stacks.with_name(stacks.name.replace('-stacks.log', '.log')).unlink(missing_ok=True)

    def on_exit(self):
        self.logger.info('PYTHON EXIT (see preceding messages for errors)')


def install(folder=None):
    from . import config
    return Diagnostics(folder or config.LOGS)
