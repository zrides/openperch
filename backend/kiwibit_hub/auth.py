"""Hub login: one account from the environment, signed session cookies, login throttling.

Sessions are stateless: a cookie carries the user and expiry, signed with a key
derived from a random server secret *and* the password, so changing the
password signs out every device. HTTP Basic stays accepted for scripts.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path

SESSION_COOKIE = 'kiwibit_session'
SESSION_SECONDS = 30 * 24 * 3600


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


def load_or_create_secret(path: Path) -> bytes:
    """A per-installation signing secret, created once with owner-only permissions."""
    path = Path(path)
    try:
        return bytes.fromhex(path.read_text(encoding='ascii').strip())
    except (FileNotFoundError, ValueError):
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_bytes(32)
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='ascii') as sink:
        sink.write(secret.hex())
    temporary.replace(path)
    return secret


class Credentials:
    def __init__(self, user, password, secret=None):
        if not password:
            raise ValueError('A password is required')
        self.user, self.password = user, password
        secret = secret or secrets.token_bytes(32)
        self._key = hmac.new(secret, b'session\0' + password.encode('utf-8'), hashlib.sha256).digest()

    @classmethod
    def from_env(cls, secret_path=None):
        password = os.environ.get('KIWIBIT_HUB_PASSWORD', '')
        if not password:
            if os.environ.get('KIWIBIT_HUB_ALLOW_NO_AUTH') == '1':
                return None
            raise RuntimeError('Set KIWIBIT_HUB_PASSWORD (or KIWIBIT_HUB_ALLOW_NO_AUTH=1 for local development). '
                               'The hub can control the camera and holds the account token.')
        if secret_path is None:
            from . import config
            secret_path = config.DATA / 'hub-secret'
        return cls(os.environ.get('KIWIBIT_HUB_USER', 'kiwibit'), password, load_or_create_secret(secret_path))

    def verify_password(self, user, password):
        # Evaluate both comparisons so timing does not reveal which one failed.
        user_ok = secrets.compare_digest((user or '').encode('utf-8'), self.user.encode('utf-8'))
        password_ok = secrets.compare_digest((password or '').encode('utf-8'), self.password.encode('utf-8'))
        return user_ok and password_ok

    def check(self, header):
        """HTTP Basic, for scripts and tools."""
        scheme, _, value = (header or '').partition(' ')
        if scheme.lower() != 'basic':
            return False
        try:
            user, _, password = base64.b64decode(value, validate=True).decode('utf-8').partition(':')
        except (binascii.Error, UnicodeDecodeError):
            return False
        return self.verify_password(user, password)

    def issue(self, lifetime=SESSION_SECONDS, now=None):
        payload = _b64(json.dumps({'u': self.user, 'exp': int((now or time.time()) + lifetime)},
                                  separators=(',', ':')).encode('utf-8'))
        return payload + '.' + _b64(hmac.new(self._key, payload.encode('ascii'), hashlib.sha256).digest())

    def verify(self, token, now=None):
        """The signed-in user for a session token, or None."""
        payload, _, signature = (token or '').partition('.')
        if not payload or not signature:
            return None
        expected = _b64(hmac.new(self._key, payload.encode('ascii'), hashlib.sha256).digest())
        if not secrets.compare_digest(signature.encode('ascii', 'replace'), expected.encode('ascii')):
            return None
        try:
            data = json.loads(_unb64(payload))
        except (ValueError, binascii.Error):
            return None
        if not isinstance(data, dict) or data.get('u') != self.user or not isinstance(data.get('exp'), int):
            return None
        return self.user if data['exp'] > (now or time.time()) else None


class LoginLimiter:
    """After `attempts` failures within `window` seconds, refuse logins for `lockout` seconds."""

    def __init__(self, attempts=5, window=300, lockout=300):
        self.attempts, self.window, self.lockout = attempts, window, lockout
        self.failures = {}
        self.locked = {}
        self.lock = threading.Lock()

    def blocked(self, key, now=None):
        now = now or time.monotonic()
        with self.lock:
            until = self.locked.get(key, 0)
            return max(0, int(until - now + 0.999))

    def failure(self, key, now=None):
        now = now or time.monotonic()
        with self.lock:
            recent = [t for t in self.failures.get(key, []) if now - t < self.window] + [now]
            self.failures[key] = recent
            if len(recent) >= self.attempts:
                self.locked[key] = now + self.lockout
                self.failures[key] = []

    def success(self, key):
        with self.lock:
            self.failures.pop(key, None)
            self.locked.pop(key, None)
