"""Scheduled cloud fetches: download new recordings and identify their birds.

A fetch only lists and downloads from the cloud; it never wakes the camera.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

log = logging.getLogger('kiwibit_hub.scheduler')

INTERVALS = (15, 30, 60, 120, 360)
DEFAULTS = {'auto': True, 'interval_minutes': 30}


class FetchScheduler:
    def __init__(self, runner, path, clock=time.time):
        self.runner = runner
        self.path = Path(path)
        self.clock = clock
        self.lock = threading.Lock()
        self.settings = dict(DEFAULTS)
        self.state = {'last_started': None, 'last_finished': None, 'last_message': '', 'last_trigger': None}
        self._load()
        runner.subscribe(self._on_job_event)

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            if isinstance(data.get('auto'), bool):
                self.settings['auto'] = data['auto']
            if data.get('interval_minutes') in INTERVALS:
                self.settings['interval_minutes'] = data['interval_minutes']
            for key in self.state:
                if key in data:
                    self.state[key] = data[key]

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps({**self.settings, **self.state}, indent=2), encoding='utf-8')
        temporary.replace(self.path)

    def configure(self, auto=None, interval_minutes=None):
        if interval_minutes is not None and interval_minutes not in INTERVALS:
            raise ValueError(f'interval_minutes must be one of {INTERVALS}')
        with self.lock:
            if auto is not None:
                self.settings['auto'] = bool(auto)
            if interval_minutes is not None:
                self.settings['interval_minutes'] = interval_minutes
            self._save()
        return self.status()

    def status(self):
        with self.lock:
            next_run = None
            if self.settings['auto']:
                last = self.state['last_started']
                next_run = self.clock() if last is None else last + self.settings['interval_minutes'] * 60
            return {**self.settings, **self.state, 'next_run': next_run, 'intervals': list(INTERVALS)}

    def tick(self):
        """Start a scheduled fetch when one is due and the hub is idle. Returns True if started."""
        with self.lock:
            if not self.settings['auto'] or self.runner.running:
                return False
            last = self.state['last_started']
            if last is not None and self.clock() - last < self.settings['interval_minutes'] * 60:
                return False
        return self.start('schedule')

    def start(self, trigger):
        try:
            self.runner.start('fetch')
        except RuntimeError:
            return False  # another job started meanwhile
        with self.lock:
            self.state.update(last_started=self.clock(), last_trigger=trigger)
            self._save()
        log.info('Fetch started (%s)', trigger)
        return True

    def _on_job_event(self, kind, **data):
        if kind == 'state' and data.get('action') == 'fetch' and not data.get('running'):
            with self.lock:
                self.state.update(last_finished=self.clock(), last_message=data.get('message', ''))
                self._save()


def run_forever(scheduler, stop, period=30):
    """Check the schedule every `period` seconds until `stop` is set."""
    while not stop.wait(period):
        try:
            scheduler.tick()
        except Exception:
            log.exception('Scheduled fetch check failed')
