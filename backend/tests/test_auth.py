import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from kiwibit_hub.auth import SESSION_COOKIE, Credentials, LoginLimiter, load_or_create_secret
from kiwibit_hub.jobs import JobRunner
from kiwibit_hub.server import create_app


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.creds = Credentials('kiwibit', 'correct horse', b'k' * 32)

    def test_session_round_trip_and_expiry(self):
        token = self.creds.issue(lifetime=60, now=1000)
        self.assertEqual(self.creds.verify(token, now=1030), 'kiwibit')
        self.assertIsNone(self.creds.verify(token, now=1061), 'expired')

    def test_tampered_or_foreign_tokens_are_rejected(self):
        token = self.creds.issue()
        payload, signature = token.split('.')
        flipped = ('B' if signature[0] == 'A' else 'A') + signature[1:]
        for bad in ('', 'x', payload + '.', '.' + signature, payload + '.' + flipped,
                    'e30.' + signature, token + 'x'):
            self.assertIsNone(self.creds.verify(bad), bad)
        other_server = Credentials('kiwibit', 'correct horse', b'z' * 32)
        self.assertIsNone(other_server.verify(token))

    def test_changing_the_password_signs_everyone_out(self):
        token = self.creds.issue()
        changed = Credentials('kiwibit', 'new password', b'k' * 32)
        self.assertIsNone(changed.verify(token))

    def test_secret_is_created_once_and_reused(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'hub-secret'
            first = load_or_create_secret(path)
            self.assertEqual(len(first), 32)
            self.assertEqual(load_or_create_secret(path), first)
            if os.name != 'nt':
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_from_env_uses_persistent_secret(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.dict(os.environ, {'KIWIBIT_HUB_PASSWORD': 'pw', 'KIWIBIT_HUB_USER': 'birder'}):
            path = Path(folder) / 'hub-secret'
            token = Credentials.from_env(path).issue()
            self.assertEqual(Credentials.from_env(path).verify(token), 'birder', 'sessions survive a restart')

    def test_limiter_locks_out_after_repeated_failures(self):
        limiter = LoginLimiter(attempts=3, window=60, lockout=120)
        for t in (0, 1, 2):
            self.assertEqual(limiter.blocked('ip', now=t), 0)
            limiter.failure('ip', now=t)
        self.assertGreater(limiter.blocked('ip', now=3), 100)
        self.assertEqual(limiter.blocked('other', now=3), 0)
        self.assertEqual(limiter.blocked('ip', now=200), 0, 'lockout expires')
        limiter.failure('ip', now=300)
        limiter.failure('ip', now=400)  # outside the window of the first
        self.assertEqual(limiter.blocked('ip', now=401), 0)


class LoginApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        web = root / 'web'
        (web / 'assets').mkdir(parents=True)
        (web / 'index.html').write_text('<!doctype html><title>Kiwibit Hub</title>')
        (web / 'assets' / 'app.js').write_text('console.log(1)')
        self.limiter = LoginLimiter(attempts=3)
        app = create_app(JobRunner(root / 'library', root / 'downloads', root),
                         Credentials('kiwibit', 'correct horse', b's' * 32), root / 'library', root,
                         diagnostics=False, web=web, limiter=self.limiter)
        self.client = TestClient(app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def login(self, password='correct horse', **extra):
        return self.client.post('/api/login', json={'username': 'kiwibit', 'password': password, **extra})

    def test_app_shell_is_public_but_api_is_not(self):
        self.assertEqual(self.client.get('/').status_code, 200)
        self.assertEqual(self.client.get('/assets/app.js').status_code, 200)
        self.assertEqual(self.client.get('/api/session').json(),
                         {'authenticated': False, 'user': None, 'auth_required': True})
        self.assertEqual(self.client.get('/api/status').status_code, 401)

    def test_login_sets_a_session_cookie_that_unlocks_api_and_events(self):
        response = self.login()
        self.assertEqual(response.status_code, 200)
        cookie = response.headers['set-cookie']
        self.assertIn(f'{SESSION_COOKIE}=', cookie)
        for flag in ('HttpOnly', 'SameSite=lax', 'Max-Age=', 'Path=/'):
            self.assertIn(flag.lower(), cookie.lower())
        self.assertEqual(self.client.get('/api/status').status_code, 200)
        self.assertEqual(self.client.get('/api/session').json()['user'], 'kiwibit')
        self.assertEqual(self.client.get('/docs').status_code, 200)
        with self.client.websocket_connect('/api/events') as socket:
            self.assertEqual(socket.receive_json()['kind'], 'state')

    def test_session_cookie_without_remember_ends_with_the_browser(self):
        cookie = self.login(remember=False).headers['set-cookie']
        self.assertNotIn('max-age', cookie.lower())

    def test_logout_clears_the_session(self):
        self.login()
        self.assertEqual(self.client.post('/api/logout').status_code, 200)
        self.assertEqual(self.client.get('/api/status').status_code, 401)
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect('/api/events') as socket:
                socket.receive_json()

    def test_wrong_password_then_lockout(self):
        for _ in range(3):
            self.assertEqual(self.login('guess').status_code, 401)
        blocked = self.login()
        self.assertEqual(blocked.status_code, 429, 'even the right password waits out the lockout')
        self.assertIn('retry-after', blocked.headers)
        self.assertNotIn('set-cookie', blocked.headers)

    def test_forged_cookie_is_rejected(self):
        self.client.cookies.set(SESSION_COOKIE, 'eyJ1Ijoia2l3aWJpdCIsImV4cCI6OTk5OTk5OTk5OX0.forged')
        self.assertEqual(self.client.get('/api/status').status_code, 401)

    def test_cross_site_requests_are_refused(self):
        self.login()
        evil = {'Origin': 'https://evil.example'}
        self.assertEqual(self.client.post('/api/live/stop', headers=evil).status_code, 403)
        self.assertEqual(self.client.post('/api/login', headers=evil,
                                          json={'username': 'kiwibit', 'password': 'correct horse'}).status_code, 403)
        same = {'Origin': 'http://testserver'}
        self.assertEqual(self.client.post('/api/jobs/cancel', headers=same).status_code, 202)
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect('/api/events', headers=evil) as socket:
                socket.receive_json()

    def test_basic_auth_still_works_for_scripts(self):
        import base64
        header = {'Authorization': 'Basic ' + base64.b64encode(b'kiwibit:correct horse').decode()}
        self.assertEqual(TestClient(self.client.app).get('/api/status', headers=header).status_code, 200)


if __name__ == '__main__':
    unittest.main()
