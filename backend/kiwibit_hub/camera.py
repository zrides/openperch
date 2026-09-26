"""The account's camera, as the hub shows it: a small, cached, credential-free summary."""
from __future__ import annotations

import threading
import time

from .kiwibit_api import KiwibitClient, load_config


class CameraService:
    def __init__(self, account=None, client=None, ttl=60):
        self._account = account
        self._client = client
        self.ttl = ttl
        self.lock = threading.Lock()
        self.cached = None
        self.fetched = 0.0

    def account(self):
        if self._account is None:
            self._account = load_config()
        return self._account

    def client(self):
        if self._client is None:
            self._client = KiwibitClient(self.account())
        return self._client

    def token(self):
        return self.account()['token'].removeprefix('Bearer ').strip()

    def device(self, refresh=False):
        """The preferred camera's raw record: the configured default, else the first."""
        with self.lock:
            if refresh or self.cached is None or time.monotonic() - self.fetched > self.ttl:
                devices = self.client().devices()
                if not devices:
                    raise LookupError('No cameras are registered on this account')
                preferred = self.account().get('defaultSerialNumber')
                self.cached = next((d for d in devices if d.get('serialNumber') == preferred), devices[0])
                self.fetched = time.monotonic()
            return self.cached

    def summary(self, refresh=False):
        device = self.device(refresh)
        return {
            'serial': device.get('serialNumber'),
            'name': device.get('deviceName') or 'Camera',
            'model': device.get('displayModelNo') or device.get('modelNo'),
            'model_number': device.get('modelNo'),
            'online': bool(device.get('online')),
            'awake': bool(device.get('awake')),
            'battery': device.get('batteryLevel'),
            'firmware': device.get('firmwareId'),
            'recording_resolution': device.get('recResolution'),
            'can_watch_live': device.get('canWatchLive', True) is not False,
        }

    def wake(self):
        device = self.device()
        self.client().wake(device['serialNumber'])
        with self.lock:
            self.fetched = 0.0  # re-read awake state on the next request
