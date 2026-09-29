"""Hub login: the owner account from the environment, accounts added on the web page,
signed session cookies, login throttling.

Sessions are stateless: a cookie carries the user and expiry, signed with a key
derived from a random server secret *and* that user's password (the owner's) or
password hash (an added user's), so changing a password signs that account out on
every device, and removing an account signs it out at once. HTTP Basic stays
accepted for scripts.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

SESSION_COOKIE = 'kiwibit_session'
SESSION_SECONDS = 30 * 24 * 3600
# Admins also manage accounts; members use everything else.
ROLES = ('admin', 'member')
USERNAME = re.compile(r'^[A-Za-z0-9._@-]{1,40}$')
MIN_PASSWORD = 8
SCRYPT = {'n': 2**14, 'r': 8, 'p': 1}


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


def _write_private(path: Path, text: str):
    """Replace a file atomically, readable by its owner only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as sink:
        sink.write(text)
    temporary.replace(path)


def load_or_create_secret(path: Path) -> bytes:
    """A per-installation signing secret, created once with owner-only permissions."""
    path = Path(path)
    try:
        return bytes.fromhex(path.read_text(encoding='ascii').strip())
    except (FileNotFoundError, ValueError):
        pass
    secret = secrets.token_bytes(32)
    _write_private(path, secret.hex())
    return secret


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode('utf-8'), salt=salt, dklen=32, **SCRYPT)
    return f"scrypt${SCRYPT['n']}${SCRYPT['r']}${SCRYPT['p']}${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        kind, n, r, p, salt, digest = stored.split('$')
        if kind != 'scrypt':
            return False
        actual = hashlib.scrypt(password.encode('utf-8'), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                                dklen=len(digest) // 2)
    except (ValueError, TypeError):
        return False
    return secrets.compare_digest(actual.hex(), digest)


# Checked when a name is unknown, so a failed sign-in takes as long either way.
_DUMMY_HASH = hash_password(secrets.token_hex(8))


def validate_password(password):
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        raise ValueError(f'Use a password of at least {MIN_PASSWORD} characters')
    if len(password) > 256:
        raise ValueError('That password is too long')


class UserStore:
    """Accounts added on the web page: a JSON file of names, roles and scrypt hashes (no passwords)."""

    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self._cache = (None, {})

    def _load(self):
        try:
            stamp = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            return {}
        if self._cache[0] != stamp:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            self._cache = (stamp, {user['name'].casefold(): user for user in data.get('users', [])})
        return self._cache[1]

    def _save(self, users):
        _write_private(self.path, json.dumps({'users': sorted(users.values(), key=lambda u: u['name'].casefold())},
                                             indent=2))
        self._cache = (None, {})

    def all(self):
        with self.lock:
            return list(self._load().values())

    def get(self, name):
        with self.lock:
            return self._load().get((name or '').casefold())

    def add(self, name, password, role):
        if not USERNAME.match(name or ''):
            raise ValueError('Use 1-40 letters, digits, dots, dashes, underscores or @ for the name')
        if role not in ROLES:
            raise ValueError(f'Role must be one of {", ".join(ROLES)}')
        validate_password(password)
        with self.lock:
            users = dict(self._load())
            if name.casefold() in users:
                raise KeyError(f'{name} already exists')
            users[name.casefold()] = {'name': name, 'role': role, 'hash': hash_password(password),
                                      'created': int(time.time())}
            self._save(users)

    def update(self, name, password=None, role=None):
        if role is not None and role not in ROLES:
            raise ValueError(f'Role must be one of {", ".join(ROLES)}')
        if password is not None:
            validate_password(password)
        with self.lock:
            users = dict(self._load())
            user = users.get((name or '').casefold())
            if user is None:
                raise LookupError(f'No account named {name}')
            user = dict(user)
            if role is not None:
                user['role'] = role
            if password is not None:
                user['hash'] = hash_password(password)
            users[user['name'].casefold()] = user
            self._save(users)

    def remove(self, name):
        with self.lock:
            users = dict(self._load())
            if users.pop((name or '').casefold(), None) is None:
                raise LookupError(f'No account named {name}')
            self._save(users)


class Credentials:
    """The owner (from the environment; always an admin) plus any accounts in `users`."""

    def __init__(self, user, password, secret=None, users=None):
        if not password:
            raise ValueError('A password is required')
        self.user, self.password = user, password
        self.users = users
        self._secret = secret or secrets.token_bytes(32)
        # The owner's key is unchanged from single-account versions: its sessions stay valid.
        self._key = hmac.new(self._secret, b'session\0' + password.encode('utf-8'), hashlib.sha256).digest()

    @classmethod
    def from_env(cls, secret_path=None, users_path=None):
        password = os.environ.get('KIWIBIT_HUB_PASSWORD', '')
        if not password:
            if os.environ.get('KIWIBIT_HUB_ALLOW_NO_AUTH') == '1':
                return None
            raise RuntimeError('Set KIWIBIT_HUB_PASSWORD (or KIWIBIT_HUB_ALLOW_NO_AUTH=1 for local development). '
                               'The hub can control the camera and holds the account token.')
        from . import config
        secret_path = secret_path or config.DATA / 'hub-secret'
        users_path = users_path or config.DATA / 'users.json'
        return cls(os.environ.get('KIWIBIT_HUB_USER', 'kiwibit'), password, load_or_create_secret(secret_path),
                   UserStore(users_path))

    def is_owner(self, name):
        return name == self.user

    def role(self, name):
        """'admin' or 'member' for a current account, else None."""
        if self.is_owner(name):
            return 'admin'
        account = self.users.get(name) if self.users else None
        return account['role'] if account else None

    def _account(self, name):
        return self.users.get(name) if self.users and not self.is_owner(name) else None

    def _key_for(self, name):
        if self.is_owner(name):
            return self._key
        account = self._account(name)
        if account is None:
            return None
        return hmac.new(self._secret, b'session\0user\0' + account['name'].encode('utf-8') + b'\0'
                        + account['hash'].encode('ascii'), hashlib.sha256).digest()

    def authenticate(self, user, password):
        """The account's name for a correct name and password, else None."""
        user, password = user or '', password or ''
        # Evaluate both owner comparisons so timing does not reveal which one failed.
        user_ok = secrets.compare_digest(user.encode('utf-8'), self.user.encode('utf-8'))
        password_ok = secrets.compare_digest(password.encode('utf-8'), self.password.encode('utf-8'))
        if user_ok:
            return self.user if password_ok else None
        account = self._account(user) if user.casefold() != self.user.casefold() else None
        if account is None:
            check_password(password, _DUMMY_HASH)
            return None
        return account['name'] if check_password(password, account['hash']) else None

    def verify_password(self, user, password):
        return self.authenticate(user, password) is not None

    def check(self, header):
        """HTTP Basic, for scripts and tools: the account's name, or None."""
        scheme, _, value = (header or '').partition(' ')
        if scheme.lower() != 'basic':
            return None
        try:
            user, _, password = base64.b64decode(value, validate=True).decode('utf-8').partition(':')
        except (binascii.Error, UnicodeDecodeError):
            return None
        return self.authenticate(user, password)

    def issue(self, user=None, lifetime=SESSION_SECONDS, now=None):
        user = user or self.user
        key = self._key_for(user)
        if key is None:
            raise LookupError(f'No account named {user}')
        payload = _b64(json.dumps({'u': user, 'exp': int((now or time.time()) + lifetime)},
                                  separators=(',', ':')).encode('utf-8'))
        return payload + '.' + _b64(hmac.new(key, payload.encode('ascii'), hashlib.sha256).digest())

    def verify(self, token, now=None):
        """The signed-in user for a session token, or None."""
        payload, _, signature = (token or '').partition('.')
        if not payload or not signature:
            return None
        try:
            data = json.loads(_unb64(payload))
        except (ValueError, binascii.Error):
            return None
        # The claimed name only chooses the key; nothing is trusted until the signature matches.
        if not isinstance(data, dict) or not isinstance(data.get('u'), str) or not isinstance(data.get('exp'), int):
            return None
        key = self._key_for(data['u'])
        if key is None:
            return None
        expected = _b64(hmac.new(key, payload.encode('ascii'), hashlib.sha256).digest())
        if not secrets.compare_digest(signature.encode('ascii', 'replace'), expected.encode('ascii')):
            return None
        return data['u'] if data['exp'] > (now or time.time()) else None


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
            if until and until <= now:
                del self.locked[key]  # expired locks don't pile up
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
